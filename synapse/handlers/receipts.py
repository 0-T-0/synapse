# -*- coding: utf-8 -*-
# Copyright 2015 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Contains handlers for federation events."""

from ._base import BaseHandler

from twisted.internet import defer

from synapse.util.logcontext import PreserveLoggingContext

import logging


logger = logging.getLogger(__name__)


class ReceiptsHandler(BaseHandler):
    def __init__(self, hs):
        super(ReceiptsHandler, self).__init__(hs)

        self.hs = hs
        self.federation = hs.get_replication_layer()
        self.federation.register_edu_handler(
            "m.receipt", self._received_remote_receipt
        )
        self.clock = self.hs.get_clock()

        self._receipt_cache = None

    @defer.inlineCallbacks
    def received_client_receipt(self, room_id, receipt_type, user_id,
                                event_id):
        # 1. Persist.
        # 2. Notify local clients
        # 3. Notify remote servers

        receipt = {
            "room_id": room_id,
            "receipt_type": receipt_type,
            "user_id": user_id,
            "event_ids": [event_id],
            "data": {
                "ts": self.clock.time_msec()
            }
        }

        is_new = yield self._handle_new_receipts([receipt])

        if is_new:
            self._push_remotes([receipt])

    @defer.inlineCallbacks
    def _received_remote_receipt(self, origin, content):
        receipts = [
            {
                "room_id": room_id,
                "receipt_type": receipt_type,
                "user_id": user_id,
                "event_ids": user_values["event_ids"],
                "data": user_values.get("data", {}),
            }
            for room_id, room_values in content.items()
            for receipt_type, users in room_values.items()
            for user_id, user_values in users.items()
        ]

        yield self._handle_new_receipts(receipts)

    @defer.inlineCallbacks
    def _handle_new_receipts(self, receipts):
        for receipt in receipts:
            room_id = receipt["room_id"]
            receipt_type = receipt["receipt_type"]
            user_id = receipt["user_id"]
            event_ids = receipt["event_ids"]
            data = receipt["data"]

            res = yield self.store.insert_receipt(
                room_id, receipt_type, user_id, event_ids, data
            )

            if not res:
                # res will be None if this read receipt is 'old'
                defer.returnValue(False)

            stream_id, max_persisted_id = res

            with PreserveLoggingContext():
                self.notifier.on_new_event(
                    "receipt_key", max_persisted_id, rooms=[room_id]
                )

            defer.returnValue(True)

    @defer.inlineCallbacks
    def _push_remotes(self, receipts):
        # TODO: Some of this stuff should be coallesced.
        for receipt in receipts:
            room_id = receipt["room_id"]
            receipt_type = receipt["receipt_type"]
            user_id = receipt["user_id"]
            event_ids = receipt["event_ids"]
            data = receipt["data"]

            remotedomains = set()

            rm_handler = self.hs.get_handlers().room_member_handler
            yield rm_handler.fetch_room_distributions_into(
                room_id, localusers=None, remotedomains=remotedomains
            )

            logger.debug("Sending receipt to: %r", remotedomains)

            for domain in remotedomains:
                self.federation.send_edu(
                    destination=domain,
                    edu_type="m.receipt",
                    content={
                        room_id: {
                            receipt_type: {
                                user_id: {
                                    "event_ids": event_ids,
                                    "data": data,
                                }
                            }
                        },
                    },
                )

    @defer.inlineCallbacks
    def get_receipts_for_room(self, room_id, to_key):
        result = yield self.store.get_linearized_receipts_for_room(
            room_id, None, to_key
        )

        if not result:
            defer.returnValue([])

        event = {
            "type": "m.receipt",
            "room_id": room_id,
            "content": result,
        }

        defer.returnValue([event])


class ReceiptEventSource(object):
    def __init__(self, hs):
        self.store = hs.get_datastore()

    @defer.inlineCallbacks
    def get_new_events_for_user(self, user, from_key, limit):
        from_key = int(from_key)
        to_key = yield self.get_current_key()

        rooms = yield self.store.get_rooms_for_user(user.to_string())
        rooms = [room.room_id for room in rooms]
        events = yield self.store.get_linearized_receipts_for_rooms(
            rooms, from_key, to_key
        )

        defer.returnValue((events, to_key))

    def get_current_key(self, direction='f'):
        return self.store.get_max_receipt_stream_id()

    @defer.inlineCallbacks
    def get_pagination_rows(self, user, config, key):
        to_key = int(config.from_key)

        if config.to_key:
            from_key = int(config.to_key)
        else:
            from_key = None

        rooms = yield self.store.get_rooms_for_user(user.to_string())
        rooms = [room.room_id for room in rooms]
        events = yield self.store.get_linearized_receipts_for_rooms(
            rooms, from_key, to_key
        )

        defer.returnValue((events, to_key))
