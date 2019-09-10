import pywingchun
import json
import http
import functools
import sys
import traceback
from itertools import groupby
import kungfu.yijinjing.time as kft
import kungfu.yijinjing.journal as kfj
import kungfu.yijinjing.msg as yjj_msg
from kungfu.yijinjing.log import create_logger
from kungfu.data.sqlite.data_proxy import LedgerDB
from kungfu.wingchun import msg
from kungfu.wingchun.finance.book import *
from kungfu.wingchun.finance.position import StockPosition, FuturePosition, FuturePositionDetail
from kungfu.wingchun.finance.position import get_uid as get_position_uid
from kungfu.wingchun.calendar import Calendar
from kungfu.wingchun.constants import OrderStatus
from kungfu.wingchun.utils import is_final_status

DEFAULT_INIT_CASH = 1e7
HANDLERS = dict()

def on(msg_type):
    def register_handler(func):
        @functools.wraps(func)
        def handler_wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        HANDLERS[msg_type] = handler_wrapper
        return on

    return register_handler

def handle(msg_type, *args, **kwargs):
    if msg_type not in HANDLERS:
        args[0].logger.error("invalid msg_type %s", msg_type)
    return HANDLERS[msg_type](*args, **kwargs)


class Ledger(pywingchun.Ledger):
    def __init__(self, ctx):
        pywingchun.Ledger.__init__(self, ctx.locator, ctx.mode, ctx.low_latency)
        self.ctx = ctx
        self.ctx.ledger = self
        self.ctx.logger = create_logger("ledger", ctx.log_level, self.io_device.home)
        self.ctx.calendar = Calendar(ctx)
        self.ctx.db = LedgerDB(self.io_device.home, ctx.name)
        self.ctx.inst_infos = {}
        self.ctx.orders = {}
        self.ctx.trading_day = None
        self.ctx.ledgers = {}
        self.ctx.get_inst_info = self.get_inst_info

    def pre_start(self):
        self.add_time_interval(1 * kft.NANO_PER_MINUTE, lambda e: self._dump_snapshot())

    def handle_request(self, event, msg):
        req = json.loads(msg)
        data = req['data']
        location = kfj.get_location_from_json(self.ctx, data)
        return json.dumps(handle(req['msg_type'], self.ctx, event, location, data))

    def on_trader_started(self, trigger_time, location):
        self.ctx.logger.info("on trader started, trigger_time:{}, uname:{}".format(trigger_time, location.uname))
        account_id = location.name
        source_id = location.group
        orders = self.ctx.db.mark_orders_status_unknown(source_id, account_id)
        for order in orders:
            self.publish(json.dumps({"msg_type": int(MsgType.Order), "data": order}))

    def on_trading_day(self, event, daytime):
        self.ctx.logger.info('on trading day %s', kft.to_datetime(daytime))
        trading_day = kft.to_datetime(daytime)
        if self.ctx.trading_day is not None and self.ctx.trading_day != trading_day:
            self._switch_day()
        for ledger in self.ctx.ledgers.values():
            ledger.apply_trading_day(trading_day)
        self.ctx.trading_day = trading_day

    def on_quote(self, event, quote):
        self.ctx.logger.debug('on quote')
        for ledger in self.ctx.ledgers.values():
            ledger.apply_quote(quote)

    def on_order(self, event, order):
        self.ctx.logger.debug('on order %s from %s', order, self.get_location(event.dest).uname)
        message = self._message_from_order_event(event, order)
        order_record = {
            'source': event.source,
            'dest': event.dest,
            'order': order
        }
        self.ctx.orders[order.order_id] = order_record
        self.ctx.db.add_order(**message["data"])
        self.publish(json.dumps(message))

    def on_trade(self, event, trade):
        self.ctx.logger.debug('on trade %s from %s', trade, self.get_location(event.dest).uname)
        client_id = self.get_location(event.dest).name
        source_id = self.get_location(event.source).group
        message = self._message_from_trade_event(event, trade)
        if source_id == "xtp" and trade.order_id in self.ctx.orders:
            self.ctx.logger.debug("update order {} by trade".format(trade.order_id))
            order_record = self.ctx.orders[trade.order_id]
            order = order_record["order"]
            if not is_final_status(order.status):
                order_message = self._message_from_order_event(event, order)
                order_message["data"]["volume_left"] = order.volume_left - trade.volume
                order_message["data"]["volume_traded"] = order.volume_traded + trade.volume
                order_message["data"]["status"] = int(OrderStatus.PartialFilledActive) if order_message["data"]["volume_left"] > 0 else int(OrderStatus.PartialFilledNotActive)
                self.ctx.db.add_order(**order_message["data"])
                self.publish(json.dumps(order_message))
            else:
                self.ctx.logger.debug("order {} enter final status {}, failed to update".format(trade.order_id, order.status))
        self.ctx.db.add_trade(**message["data"])
        self.publish(json.dumps(message))

        self._get_ledger(ledger_category=LedgerCategory.Account, source_id=source_id,account_id=trade.account_id).apply_trade(trade)
        self._get_ledger(ledger_category=LedgerCategory.Portfolio, client_id=client_id).apply_trade(trade)

    def on_instruments(self, instruments):
        inst_list = list(set(instruments))
        if inst_list:
            self.ctx.db.set_instruments([object_as_dict(inst) for inst in inst_list])
            self.ctx.inst_infos = {inst.instrument_id: object_as_dict(inst) for inst in inst_list}

    def on_stock_account(self, asset, positions):
        self.ctx.logger.info("asset: {}".format(asset))
        for pos in positions:
            self.ctx.logger.info("pos: {}".format(pos))
        pos_objects = [StockPosition(**object_as_dict(pos)) for pos in positions]
        account = AccountBook(ctx=self.ctx,
                              trading_day=self.ctx.trading_day,
                              ledger_category=LedgerCategory.Account,
                              account_id=asset.account_id,
                              source_id=asset.source_id,
                              avail=asset.avail,
                              positions=pos_objects)
        ledger = self._get_ledger(ledger_category=LedgerCategory.Account, source_id = asset.source_id, account_id=asset.account_id).merge(account)
        self.publish(json.dumps(ledger.message))
        self.ctx.db.dump(ledger)

    def on_future_account(self, asset, position_details):
        pos_objects = []
        for uid, details in groupby(position_details, key=lambda e: get_position_uid(e.instrument_id, e.exchange_id, e.direction)):
            detail_list = list(details)
            direction = detail_list[0].direction
            instrument_id = detail_list[0].instrument_id
            exchange_id = detail_list[0].exchange_id
            instrument_info = self.ctx.db.get_instrument_info(instrument_id)
            detail_objects = []
            for detail in sorted(detail_list, key=lambda detail: (detail.open_date, detail.trade_time)):
                args = object_as_dict(detail)
                args.update({"contract_multiplier": instrument_info["contract_multiplier"],
                             "long_margin_ratio": instrument_info["long_margin_ratio"],
                             "short_margin_ratio": instrument_info["short_margin_ratio"]})
                detail_objects.append(FuturePositionDetail(**args))
            pos_args = {"instrument_id": instrument_id, "exchange_id": exchange_id, "details": detail_objects, "trading_day": self.ctx.trading_day, "direction": direction}
            pos_args.update(instrument_info)
            pos = FuturePosition(**pos_args)
            pos_objects.append(pos)
        account_book = AccountBook(ctx=self.ctx,
                                   trading_day=self.ctx.trading_day,
                                   ledger_category=LedgerCategory.Account,
                                   account_id=asset.account_id,
                                   source_id=asset.source_id,
                                   avail=asset.avail,
                                   positions=pos_objects)
        ledger = self._get_ledger(ledger_category=LedgerCategory.Account, source_id=asset.source_id,account_id=asset.account_id).merge(account_book)
        self.publish(json.dumps(ledger.message))
        self.ctx.db.dump(ledger)

    def _message_from_order_event(self, event, order):
        order_dict = object_as_dict(order)
        order_dict["order_id"] = str(order.order_id)
        order_dict["parent_id"] = str(order.parent_id)
        order_dict["client_id"] = self.get_location(event.dest).name
        return {"msg_type": int(MsgType.Order), "data": order_dict}

    def _message_from_trade_event(self, event, trade):
        client_id = self.get_location(event.dest).name
        trade_dict = object_as_dict(trade)
        trade_dict["order_id"] = str(trade.order_id)
        trade_dict["parent_order_id"] = str(trade.parent_order_id)
        trade_dict["trade_id"] = str(trade.trade_id)
        trade_dict["client_id"] = client_id
        return {"msg_type": int(MsgType.Trade), "data": trade_dict}

    def _dump_snapshot(self, data_frequency="minute"):
        messages = []
        for ledger in self.ctx.ledgers.values():
            message = ledger.message
            message["msg_type"] = int(MsgType.AssetSnapshot)
            tags = {"update_time": self.now(), "data_frequency": data_frequency}
            message["data"].update(tags)
            self.publish(json.dumps(message))
            messages.append(message)
        self.ctx.db.on_messages(messages)

    def _switch_day(self):
        self.publish(json.dumps({
            'msg_type': msg.Calendar,
            'data': {
                'trading_day': '%s' % self.ctx.calendar.trading_day
            }
        }))
        self._dump_snapshot(data_frequency="daily")
        for ledger in self.ctx.ledgers.values():
            self.ctx.db.dump(ledger)

    def has_ledger(self, ledger_category, source_id="", account_id="", client_id=""):
        uid = AccountBook.get_uid(category=ledger_category,source_id=source_id, account_id=account_id, client_id=client_id)
        return uid in self.ctx.ledgers

    def pop_ledger(self, ledger_category, source_id="", account_id="", client_id=""):
        uid = AccountBook.get_uid(category=ledger_category,source_id=source_id, account_id=account_id, client_id=client_id)
        return self.ctx.ledgers.pop(uid, None)

    def _get_ledger(self, ledger_category, source_id="", account_id="", client_id=""):
        uid = AccountBook.get_uid(category=ledger_category,source_id=source_id, account_id=account_id, client_id=client_id)
        uname = AccountBook.get_uname(category=ledger_category, source_id=source_id, account_id=account_id, client_id=client_id)
        if uid not in self.ctx.ledgers:
            ledger = self.ctx.db.load(ctx = self.ctx, ledger_category=ledger_category, source_id=source_id,account_id=account_id, client_id=client_id)
            if not ledger:
                self.ctx.logger.info("failed to load ledger {} from sqlite".format(uname))
                ledger = AccountBook(self.ctx, ledger_category=ledger_category, source_id=source_id, account_id=account_id, client_id=client_id, avail=DEFAULT_INIT_CASH, trading_day=self.ctx.trading_day)
            ledger._ctx = self.ctx
            ledger.apply_trading_day(self.ctx.trading_day)
            ledger.register_callback(lambda messages: [self.publish(json.dumps(message)) for message in messages])
            ledger.register_callback(self.ctx.db.on_messages)
            self.ctx.ledgers[uid] = ledger
            self.ctx.logger.info("success to init ledger, uid: {}, uname: {}".format(uid, uname))
        return self.ctx.ledgers[uid]

    def get_inst_info(self, instrument_id):
        if not instrument_id in self.ctx.inst_infos:
            self.ctx.inst_infos[instrument_id] = self.ctx.db.get_instrument_info(instrument_id)
        return self.ctx.db.get_instrument_info(instrument_id)

@on(msg.Calendar)
def calendar_request(ctx, event, location, data):
    message = {
        'msg_type': msg.Calendar,
        'data': {
            'trading_day': ctx.calendar.trading_day.strftime("%Y%m%d"),
        }
    }
    ctx.ledger.publish(json.dumps(message))
    message.update({'status': http.HTTPStatus.OK})
    return message

@on(msg.BrokerStateRefresh)
def broker_state_refresh(ctx, event, location, data):
    ctx.ledger.publish_broker_states(event.gen_time)
    return {
        'status': http.HTTPStatus.OK,
        'msg_type': msg.BrokerStateRefresh
    }

@on(msg.NewOrderSingle)
def new_order_single(ctx, event, location, data):
    # ctx.ledger.new_order_single(event, location.uid)
    return {
        'status': http.HTTPStatus.OK,
        'msg_type': msg.NewOrderSingle
    }

@on(msg.CancelOrder)
def cancel_order(ctx, event, location, data):
    ctx.logger.info('cancel account order request')
    order_id = int(data['order_id'])
    if order_id in ctx.orders:
        order_record = ctx.orders[order_id]
        dest = order_record["dest"]
        source = order_record["source"]
        ctx.logger.info('cancel account order dest: %s, source: %s, order: %s', order_record["dest"], order_record["source"], order_record['order'])
        try:
            ctx.ledger.cancel_order(event, source, order_id)
            return {'status': http.HTTPStatus.OK,'msg_type': msg.CancelOrder}
        except Exception as err:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            ctx.logger.error('failed to cancel order %s, error [%s] %s', order_id, exc_type, traceback.format_exception(exc_type, exc_obj, exc_tb))
            return {'status': http.HTTPStatus.NOT_FOUND,'msg_type': msg.CancelOrder}
    else:
        ctx.logger.error('can not cancel order %s from orders %s', order_id, list(ctx.orders.keys()))
        return {
            'status': http.HTTPStatus.NOT_FOUND,
            'msg_type': msg.CancelOrder
        }

@on(msg.CancelAllOrder)
def cancel_all_order(ctx, event, location, data):
    ctx.logger.info('cancel all account order request')
    for order_id in ctx.orders:
        order_record = ctx.orders[order_id]
        if order_record['source'] == location.uid:
            ctx.logger.info('cancel account order %s', order_record['order'])
            ctx.ledger.cancel_order(event, location.uid, order_id)
        if order_record['dest'] == location.uid:
            source = order_record["source"]
            if ctx.ledger.has_location(source):
                ctx.logger.info('cancel strategy order %s', order_record['order'])
                ctx.ledger.cancel_order(event, source, order_id)
            else:
                ctx.logger.warn("failed to find location {}".format(source))
    return {
        'status': http.HTTPStatus.OK,
        'msg_type': msg.CancelAllOrder
    }

@on(msg.QryAsset)
def qry_asset(ctx, event, location, data):
    ctx.logger.info("qry asset, input: {}".format(data))
    uid = AccountBook.get_uid(category=data["ledger_category"], source_id=data["source_id"],account_id=data["account_id"],client_id=data["client_id"])
    if uid in ctx.ledgers:
        message = ctx.ledgers[uid].message
        message.update({'status': http.HTTPStatus.OK, 'msg_type': msg.QryAsset})
        return message
    else:
        return {
            'status': http.HTTPStatus.NOT_FOUND,
            'msg_type': msg.QryAsset
        }

@on(msg.PublishAllAssetInfo)
def publish_all_asset(ctx, event, location, data):
    ctx.logger.info("req publish all recorded asset info")
    for ledger in ctx.ledgers.values():
        ctx.ledger.publish(json.dumps(ledger.message))
    return {
        'status': http.HTTPStatus.OK,
        'msg_type': msg.PublishAllAssetInfo
    }

@on(msg.RemoveStrategy)
def remove_strategy(ctx, event, location, data):
    if location is None:
        ctx.logger.warn("location is None, data: {}".format(data))
        return {
            'status': http.HTTPStatus.NOT_FOUND,
            'msg_type': msg.RemoveStrategy
        }
    elif ctx.ledger.has_location(location.uid):
        ctx.logger.warn("strategy is running, failed to delete")
        return {
            'status': http.HTTPStatus.NOT_FOUND,
            'msg_type': msg.RemoveStrategy
        }
    else:
        ctx.ledger.pop_ledger(LedgerCategory.Portfolio, client_id=data["name"])
        ctx.db.remove(LedgerCategory.Portfolio, client_id=data["name"])
        return {
            'status': http.HTTPStatus.OK,
            'msg_type': msg.RemoveStrategy
        }

@on(yjj_msg.TradingDay)
def update_trading_day(ctx, event, location, data):
    ctx.logger.warn("switch trading day from UI for test")
    daytime = data["daytime"]
    ctx.ledger.on_trading_day(event, daytime)
    return {'status': http.HTTPStatus.OK, 'msg_type': yjj_msg.TradingDay}