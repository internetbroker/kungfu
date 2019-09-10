from kungfu.wingchun.finance.position import StockPosition, FuturePosition
from kungfu.wingchun.finance.position import get_uid as get_position_uid
from kungfu.wingchun.utils import *
from kungfu.wingchun.constants import *
import datetime
from pyyjj import hash_str_32
import sys
import traceback

class AccountBook:
    def __init__(self, ctx, **kwargs):
        self._ctx = ctx        
        self._trading_day = kwargs.pop("trading_day")
        if isinstance(self._trading_day, str):
            self._trading_day = datetime.datetime.strptime(self._trading_day, "%Y%m%d")
        self._initial_equity = kwargs.pop("initial_equity", 0.0)
        self._static_equity = kwargs.pop("static_equity", 0.0)
        self._avail = kwargs.pop("avail", 0.0)
        self._realized_pnl = kwargs.pop("realized_pnl", 0.0)
        positions = kwargs.pop("positions", [])
        for pos in positions:
            pos.ledger = self
        self._positions = {pos.uid: pos for pos in positions}

        if self._initial_equity <= 0.0:
            self._initial_equity = self.dynamic_equity # fill initial equity
        if self._static_equity <= 0.0:
            self._static_equity = self.dynamic_equity

        self._category = kwargs.pop("ledger_category", int(LedgerCategory.Unknown))
        self._account_id = kwargs.pop("account_id", '')
        self._client_id = kwargs.pop("client_id", '')
        self._source_id = kwargs.pop("source_id", '')
        self._uname = AccountBook.get_uname(self._category, source_id=self._source_id, account_id=self._account_id,client_id=self._client_id)
        self._uid = AccountBook.get_uid(self._category, source_id=self._source_id, account_id=self._account_id,client_id=self._client_id)

        self._notice_threshold = kwargs.pop("notice_threshold", 10)

        self._tags = {"ledger_category": int(self.category),
                      "account_id": self.account_id,
                      "client_id": self.client_id,
                      "source_id": self.source_id
                      }

        self._callbacks = []

    @classmethod
    def get_uname(cls, category, source_id = "", account_id = "", client_id=""):
        return "{}.{}.{}.{}".format(int(category), source_id, account_id, client_id)

    @classmethod
    def get_uid(cls, category, source_id = "", account_id = "", client_id=""):
        uname = cls.get_uname(category, source_id, account_id, client_id)
        return hash_str_32(uname)

    @property
    def category(self):
        return self._category

    @property
    def account_id(self):
        return self._account_id

    @property
    def client_id(self):
        return self._client_id

    @property
    def source_id(self):
        return self._source_id

    @property
    def uname(self):
        return AccountBook.get_uname(self.category, self.source_id, self.account_id, self.client_id)

    @property
    def avail(self):
        return self._avail

    @avail.setter
    def avail(self, value):
        self._avail = value

    @property
    def trading_day(self):
        return self._trading_day

    @property
    def message(self):
        return {
            "msg_type": int(MsgType.Asset),
            "data": {
                "ledger_category": int(self.category),
                "trading_day": self.trading_day.strftime("%Y%m%d"),
                "account_id": self.account_id,
                "client_id": self.client_id,
                "source_id": self.source_id,
                "avail": self.avail,
                "margin": self.margin,
                "market_value": self.market_value,
                "initial_equity": self.initial_equity,
                "dynamic_equity": self.dynamic_equity,
                "static_equity": self.static_equity,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": self.unrealized_pnl
            }
        }

    @property
    def detail_messages(self):
        messages = [self.message]
        for pos in self.positions:
            messages.append(pos.message)
            if isinstance(pos, FuturePosition):
                for detail in pos.details:
                    if detail.volume > 0: messages.append(detail.message)
        for message in messages:
            self.fill_msg_tag(message)
        return messages

    @property
    def positions(self):
        return list(self._positions.values())

    @property
    def margin(self):
        return sum([position.margin for position in self._positions.values()])

    @property
    def market_value(self):
        return sum([position.market_value for position in self._positions.values() if isinstance(position, StockPosition)])

    @property
    def initial_equity(self):
        return self._initial_equity

    @property
    def static_equity(self):
        return self._static_equity

    @property
    def dynamic_equity(self):
        total_value = self.avail
        for pos in self.positions:
            if isinstance(pos, FuturePosition):
                total_value += (pos.margin + pos.position_pnl)
            elif isinstance(pos, StockPosition):
                total_value += pos.market_value
        return total_value

    @property
    def realized_pnl(self):
        return self._realized_pnl

    @realized_pnl.setter
    def realized_pnl(self, value):
        self._realized_pnl = value

    @property
    def unrealized_pnl(self):
        return sum([position.unrealized_pnl for position in self._positions.values()])

    def __repr__(self):
        return "%s(%r)" % (self.__class__, self.message["data"])

    def register_callback(self, callback):
        self._callbacks.append(callback)

    def dispatch(self, messages):
        for msg in messages:
            self.fill_msg_tag(msg)
        self._ctx.logger.debug("dispatch messages: %s for callbacks: %s", messages, self._callbacks)
        for cb in self._callbacks:
            cb(messages)

    def fill_msg_tag(self, message):
        message["data"].update(self._tags)

    def merge(self, ledger):
        self._ctx.logger.info("merge {} with {}".format(self, ledger))
        if self.realized_pnl == 0.0 and ledger.realized_pnl != 0.0:
            self.realized_pnl = ledger.realized_pnl
        if self._static_equity == 1e7 and ledger.static_equity != 1e7:
            self._static_equity = ledger.static_equity
        if self._initial_equity == 1e7 and ledger.initial_equity != 1e7:
            self._initial_equity = ledger.initial_equity
        self.avail = ledger.avail
        for symbol in set(self._positions.keys()).union(set(ledger._positions.keys())):
            l_pos = self._positions.pop(symbol, None)
            r_pos = ledger._positions.get(symbol, None)
            if l_pos and r_pos:
                self._ctx.logger.info("merge position {} with {}".format(l_pos, r_pos))
                l_pos.merge(r_pos)
                self._positions[symbol] = l_pos
            elif not l_pos and r_pos:
                r_pos.ledger = self
                self._positions[symbol] = r_pos
                self._ctx.logger.info("add position {}".format(r_pos))
            elif l_pos and not r_pos:
                self._ctx.logger.info("delete position {}".format(l_pos))
        self._ctx.logger.info("merged {}".format(self))
        return self

    def get_position(self, instrument_id, exchange_id, direction = Direction.Long):
        uid = get_position_uid(instrument_id, exchange_id, direction)
        return self._positions.get(uid, None)

    def apply_quote(self, quote):
        self._ctx.logger.debug("{} apply quote".format(self.uname))
        position = self.get_position(quote.instrument_id, quote.exchange_id, Direction.Long)
        if position is not None:
            position.apply_quote(quote)
        position = self.get_position(quote.instrument_id, quote.exchange_id, Direction.Short)
        if position is not None:
            position.apply_quote(quote)

    def apply_trade(self, trade):
        self._ctx.logger.info("{} apply trade[(trade_id){} (inst){} (price){} (volume){}]".format(self.uname, trade.trade_id, trade.instrument_id, trade.price, trade.volume))
        instrument_type = get_instrument_type(trade.instrument_id, trade.exchange_id)
        direction = get_position_effect(instrument_type, trade.side, trade.offset)
        try:
            self._get_position(trade.instrument_id, trade.exchange_id, direction).apply_trade(trade)
        except Exception as err:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            self._ctx.logger.error('apply trade error [%s] %s', exc_type, traceback.format_exception(exc_type, exc_obj, exc_tb))

    def apply_trading_day(self, trading_day):
        if not self.trading_day == trading_day:
            self._ctx.logger.info("{} apply trading day, switch from {} to {}".format(self.uname, self.trading_day, trading_day))
            self._trading_day = trading_day
            for pos in self._positions.values():
                self._ctx.logger.info("position {} for {} switch trading day from {} to {}".format(pos.uname, self.uname, pos.trading_day, trading_day))
                pos.switch_day(trading_day)
            self._static_equity = self.dynamic_equity
            self.dispatch([self.message])
            self.dispatch(self.detail_messages)
        else:
            self._ctx.logger.debug("{} receive duplicate trading_day message {}".format(self.uname, trading_day))

    def _get_position(self, instrument_id, exchange_id, direction = Direction.Long):
        uid = get_position_uid(instrument_id, exchange_id, direction)
        if uid not in self._positions:
            instrument_type = get_instrument_type(instrument_id, exchange_id)            
            if instrument_type == InstrumentType.Stock:
                position = StockPosition(ledger = self,instrument_id = instrument_id, exchange_id = exchange_id, trading_day = self.trading_day)
            else:
                instrument_info = self._ctx.get_inst_info(instrument_id)
                margin_ratio = instrument_info["short_margin_ratio"] if direction == Direction.Short else instrument_info["long_margin_ratio"]
                position = FuturePosition(ledger = self,
                                          instrument_id = instrument_id,
                                          exchange_id = exchange_id,
                                          trading_day = self.trading_day,
                                          margin_ratio = margin_ratio,
                                          direction = direction,
                                          contract_multiplier = instrument_info["contract_multiplier"])
            self._positions[uid] = position
        return self._positions[uid]

