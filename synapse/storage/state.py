# -*- coding: utf-8 -*-
# Copyright 2014, 2015 OpenMarket Ltd
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

from ._base import SQLBaseStore

from twisted.internet import defer

from synapse.util.stringutils import random_string

import logging

logger = logging.getLogger(__name__)


class StateStore(SQLBaseStore):
    """ Keeps track of the state at a given event.

    This is done by the concept of `state groups`. Every event is a assigned
    a state group (identified by an arbitrary string), which references a
    collection of state events. The current state of an event is then the
    collection of state events referenced by the event's state group.

    Hence, every change in the current state causes a new state group to be
    generated. However, if no change happens (e.g., if we get a message event
    with only one parent it inherits the state group from its parent.)

    There are three tables:
      * `state_groups`: Stores group name, first event with in the group and
        room id.
      * `event_to_state_groups`: Maps events to state groups.
      * `state_groups_state`: Maps state group to state events.
    """

    def get_state_groups(self, event_ids):
        """ Get the state groups for the given list of event_ids

        The return value is a dict mapping group names to lists of events.
        """

        def f(txn):
            groups = set()
            for event_id in event_ids:
                group = self._simple_select_one_onecol_txn(
                    txn,
                    table="event_to_state_groups",
                    keyvalues={"event_id": event_id},
                    retcol="state_group",
                    allow_none=True,
                )
                if group:
                    groups.add(group)

            res = {}
            for group in groups:
                res[group] = self._retrieve_events(
                    txn,
                    "INNER JOIN state_groups_state as s ON s.event_id = ej.event_id"
                    " WHERE s.state_group = ?",
                    (group,)
                )

            return res

        return self.runInteraction(
            "get_state_groups",
            f,
        )

    def _store_state_groups_txn(self, txn, event, context):
        if context.current_state is None:
            return

        state_events = dict(context.current_state)

        if event.is_state():
            state_events[(event.type, event.state_key)] = event

        state_group = context.state_group
        if not state_group:
            state_group = self._state_groups_id_gen.get_next_txn(txn)
            self._simple_insert_txn(
                txn,
                table="state_groups",
                values={
                    "id": state_group,
                    "room_id": event.room_id,
                    "event_id": event.event_id,
                },
            )

            for state in state_events.values():
                self._simple_insert_txn(
                    txn,
                    table="state_groups_state",
                    values={
                        "state_group": state_group,
                        "room_id": state.room_id,
                        "type": state.type,
                        "state_key": state.state_key,
                        "event_id": state.event_id,
                    },
                )

        self._simple_insert_txn(
            txn,
            table="event_to_state_groups",
            values={
                "state_group": state_group,
                "event_id": event.event_id,
            },
        )

    @defer.inlineCallbacks
    def get_current_state(self, room_id, event_type=None, state_key=""):
        del_sql = (
            "SELECT event_id FROM redactions WHERE redacts = e.event_id "
            "LIMIT 1"
        )

        sql = (
            "SELECT e.*, (%(redacted)s) AS redacted FROM events as e "
            "INNER JOIN current_state_events as c ON e.event_id = c.event_id "
            "WHERE c.room_id = ? "
        ) % {
            "redacted": del_sql,
        }

        if event_type and state_key is not None:
            sql += " AND c.type = ? AND c.state_key = ? "
            args = (room_id, event_type, state_key)
        elif event_type:
            sql += " AND c.type = ?"
            args = (room_id, event_type)
        else:
            args = (room_id, )

        results = yield self._execute_and_decode("get_current_state", sql, *args)

        events = yield self._parse_events(results)
        defer.returnValue(events)


def _make_group_id(clock):
    return str(int(clock.time_msec())) + random_string(5)
