//
// Created by Keren Dong on 2019-06-20.
//

#include <fmt/format.h>
#include <hffix.hpp>

#include <kungfu/yijinjing/log/setup.h>
#include <kungfu/yijinjing/time.h>
#include <kungfu/yijinjing/msg.h>
#include <kungfu/wingchun/strategy/context.h>
#include <kungfu/wingchun/utils.h>

using namespace kungfu::practice;
using namespace kungfu::rx;
using namespace kungfu::yijinjing;
using namespace kungfu::yijinjing::data;
using namespace kungfu::wingchun::msg::data;

namespace kungfu
{
    namespace wingchun
    {
        namespace strategy
        {
            Context::Context(practice::apprentice &app, const rx::connectable_observable<yijinjing::event_ptr> &events) :
                    app_(app), events_(events)
            {
                auto home = app.get_io_device()->get_home();
                log::copy_log_settings(home, home->name);
            }

            void Context::react()
            {
                events_ | is(msg::type::Quote) |
                $([&](event_ptr event)
                  {
                      const Quote &quote = event->data<Quote>();
                      auto id = get_symbol_id(quote.get_instrument_id(), quote.get_exchange_id());
                      quotes_[id].last_price = quote.last_price;
                  });

                events_ | is(msg::type::Order) |
                $([&](event_ptr event)
                  {
                      auto order = event->data<Order>();
                  });

                events_ | is(msg::type::Trade) |
                $([&](event_ptr event)
                  {
                      auto trade = event->data<Trade>();
                  });

                events_ | is(msg::type::Entrust) |
                $([&](event_ptr event)
                  {
                      auto entrust = event->data<Entrust>();
                  });

                events_ | is(msg::type::Transaction) |
                $([&](event_ptr event)
                  {
                      auto transaction = event->data<Transaction>();
                  });
            }

            int64_t Context::now() const
            {
                return app_.now();
            }

            void Context::add_timer(int64_t nanotime, const std::function<void(yijinjing::event_ptr)>& callback)
            {
                app_.add_timer(nanotime, callback);
            }

            void Context::add_time_interval(int64_t duration, const std::function<void(yijinjing::event_ptr)>& callback)
            {
                app_.add_time_interval(duration, callback);
            }

            void Context::add_account(const std::string &source, const std::string &account, double cash_limit)
            {
                uint32_t account_id = yijinjing::util::hash_str_32(account);
                if (accounts_.find(account_id) != accounts_.end())
                {
                    throw wingchun_error(fmt::format("duplicated account {}@{}", account, source));
                }

                auto home = app_.get_io_device()->get_home();
                auto account_location = location::make(mode::LIVE, category::TD, source, account, home->locator);
                if (not app_.has_location(account_location->uid))
                {
                    throw wingchun_error(fmt::format("invalid account {}@{}", account, source));
                }
                account_location_ids_[account_id] = account_location->uid;

                app_.request_write_to(app_.now(), account_location->uid);
                app_.request_read_from(app_.now(), account_location->uid, true);
                app_.request_read_from(app_.now(), account_location->uid);
                SPDLOG_INFO("added account {}@{} [{:08x}]", account, source, account_id);
            }

            void Context::subscribe(const std::string &source, const std::vector<std::string> &symbols, const std::string &exchange)
            {
                if (market_data_.find(source) == market_data_.end())
                {
                    auto home = app_.get_io_device()->get_home();
                    auto md_location = location::make(mode::LIVE, category::MD, source, source, home->locator);
                    if (not app_.has_location(md_location->uid))
                    {
                        throw wingchun_error(fmt::format("invalid md {}", source));
                    }
                    app_.request_read_from(app_.now(), md_location->uid, true);
                    app_.request_write_to(app_.now(), md_location->uid);
                    market_data_[source] = md_location->uid;
                    SPDLOG_INFO("added md {} [{:08x}]", source, md_location->uid);
                }
                uint32_t md_source = market_data_[source];
                SPDLOG_INFO("strategy subscribe from {} [{:08x}]", source, md_source);
                if (not app_.has_writer(md_source))
                {
                    events_ | is(yijinjing::msg::type::RequestWriteTo) |
                    filter([=](yijinjing::event_ptr e)
                           {
                               const yijinjing::msg::data::RequestWriteTo &data = e->data<yijinjing::msg::data::RequestWriteTo>();
                               return data.dest_id == md_source;
                           }) | first() |
                    $([=](event_ptr e)
                      {
                          request_subscribe(md_source, symbols, exchange);
                      });
                } else
                {
                    request_subscribe(md_source, symbols, exchange);
                }
            }

            void Context::request_subscribe(uint32_t source, const std::vector<std::string> &symbols, const std::string &exchange)
            {
                auto writer = app_.get_writer(source);
                char *buffer = const_cast<char *>(&(writer->open_frame(app_.now(), msg::type::Subscribe, 4096)->data<char>()));
                size_t length = fill_subscribe_msg(buffer, 4096, symbols, exchange);
                writer->close_frame(length);
            }

            uint64_t Context::insert_order(const std::string &symbol, const std::string &exchange, const std::string &account,
                                                 double limit_price, int64_t volume, PriceType type, Side side, Offset offset)
            {
                auto writer = app_.get_writer(lookup_account_location_id(account));
                msg::data::OrderInput &input = writer->open_data<msg::data::OrderInput>(0, msg::type::OrderInput);
                input.order_id = writer->current_frame_uid();
                strcpy(input.instrument_id, symbol.c_str());
                strcpy(input.exchange_id, exchange.c_str());
                strcpy(input.account_id, account.c_str());
                input.limit_price = limit_price;
                input.frozen_price = limit_price;
                input.volume = volume;
                input.price_type = type;
                input.side = side;
                input.offset = offset;
                writer->close_data();
                return input.order_id;
            }

            uint64_t Context::cancel_order(uint64_t order_id)
            {
                uint32_t account_location_id = (order_id >> 32) ^ app_.get_home_uid();
                SPDLOG_INFO("{:08x} cancel order {:016x} with account location {:08x}", app_.get_home_uid(), order_id, account_location_id);
                auto writer = app_.get_writer(account_location_id);
                msg::data::OrderAction &action = writer->open_data<msg::data::OrderAction>(0, msg::type::OrderAction);

                action.order_action_id = writer->current_frame_uid();
                action.order_id = order_id;
                action.action_flag = OrderActionFlag::Cancel;

                writer->close_data();
                return action.order_action_id;
            }

            uint32_t Context::lookup_account_location_id(const std::string &account)
            {
                uint32_t account_id = yijinjing::util::hash_str_32(account);
                if (account_location_ids_.find(account_id) == account_location_ids_.end())
                {
                    throw wingchun_error("invalid account " + account);
                }
                return account_location_ids_[account_id];
            }
        }
    }
}