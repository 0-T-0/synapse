# -*- coding: utf-8 -*-
# Copyright 2014 OpenMarket Ltd
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

from twisted.internet import defer

from synapse.api.errors import LimitExceededError, SynapseError
from synapse.util.async import run_on_reactor
from synapse.crypto.event_signing import add_hashes_and_signatures
from synapse.api.constants import Membership, EventTypes

from synapse.events.snapshot import EventContext

import logging


logger = logging.getLogger(__name__)


class BaseHandler(object):

    def __init__(self, hs):
        self.store = hs.get_datastore()
        self.event_factory = hs.get_event_factory()
        self.auth = hs.get_auth()
        self.notifier = hs.get_notifier()
        self.room_lock = hs.get_room_lock_manager()
        self.state_handler = hs.get_state_handler()
        self.distributor = hs.get_distributor()
        self.ratelimiter = hs.get_ratelimiter()
        self.clock = hs.get_clock()
        self.hs = hs

        self.signing_key = hs.config.signing_key[0]
        self.server_name = hs.hostname

        self.event_builder_factory = hs.get_event_builder_factory()

    def ratelimit(self, user_id):
        time_now = self.clock.time()
        allowed, time_allowed = self.ratelimiter.send_message(
            user_id, time_now,
            msg_rate_hz=self.hs.config.rc_messages_per_second,
            burst_count=self.hs.config.rc_message_burst_count,
        )
        if not allowed:
            raise LimitExceededError(
                retry_after_ms=int(1000*(time_allowed - time_now)),
            )

    @defer.inlineCallbacks
    def _create_new_client_event(self, builder):
        yield run_on_reactor()

        context = EventContext()

        latest_ret = yield self.store.get_latest_events_in_room(
            builder.room_id,
        )

        if latest_ret:
            depth = max([d for _, _, d in latest_ret])
        else:
            depth = 1

        prev_events = [(e, h) for e, h, _ in latest_ret]

        builder.prev_events = prev_events
        builder.depth = depth

        state_handler = self.state_handler
        ret = yield state_handler.annotate_context_with_state(
            builder,
            context,
        )
        prev_state = ret

        if builder.is_state():
            builder.prev_state = prev_state

        yield self.auth.add_auth_events(builder, context)

        add_hashes_and_signatures(
            builder, self.server_name, self.signing_key
        )

        event = builder.build()

        defer.returnValue(
            (event, context,)
        )

    @defer.inlineCallbacks
    def handle_new_client_event(self, event, context, extra_destinations=[],
                                extra_users=[], suppress_auth=False):
        yield run_on_reactor()

        # We now need to go and hit out to wherever we need to hit out to.

        if not suppress_auth:
            self.auth.check(event, auth_events=context.auth_events)

        yield self.store.persist_event(event, context=context)

        federation_handler = self.hs.get_handlers().federation_handler

        if event.type == EventTypes.Member:
            if event.content["membership"] == Membership.INVITE:
                invitee = self.hs.parse_userid(event.state_key)
                if not self.hs.is_mine(invitee):
                    returned_invite = yield federation_handler.send_invite(
                        invitee.domain,
                        event,
                    )
                    event.signatures.update(
                        returned_invite.signatures
                    )

        destinations = set(extra_destinations)
        for k, s in context.current_state.items():
            try:
                if k[0] == EventTypes.Member:
                    if s.content["membership"] == Membership.JOIN:
                        destinations.add(
                            self.hs.parse_userid(s.state_key).domain
                        )
            except SynapseError:
                logger.warn(
                    "Failed to get destination from event %s", s.event_id
                )

        yield self.notifier.on_new_room_event(event, extra_users=extra_users)

        yield federation_handler.handle_new_event(
            event,
            None,
            destinations=destinations,
        )

    # @defer.inlineCallbacks
    # def _on_new_room_event(self, event, snapshot, extra_destinations=[],
    #                        extra_users=[], suppress_auth=False,
    #                        do_invite_host=None):
    #     yield run_on_reactor()
    #
    #     snapshot.fill_out_prev_events(event)
    #
    #     yield self.state_handler.annotate_event_with_state(event)
    #
    #     yield self.auth.add_auth_events(event)
    #
    #     logger.debug("Signing event...")
    #
    #     add_hashes_and_signatures(
    #         event, self.server_name, self.signing_key
    #     )
    #
    #     logger.debug("Signed event.")
    #
    #     if not suppress_auth:
    #         logger.debug("Authing...")
    #         self.auth.check(event, auth_events=event.old_state_events)
    #         logger.debug("Authed")
    #     else:
    #         logger.debug("Suppressed auth.")
    #
    #     if do_invite_host:
    #         federation_handler = self.hs.get_handlers().federation_handler
    #         invite_event = yield federation_handler.send_invite(
    #             do_invite_host,
    #             event
    #         )
    #
    #         # FIXME: We need to check if the remote changed anything else
    #         event.signatures = invite_event.signatures
    #
    #     yield self.store.persist_event(event)
    #
    #     destinations = set(extra_destinations)
    #     # Send a PDU to all hosts who have joined the room.
    #
    #     for k, s in event.state_events.items():
    #         try:
    #             if k[0] == RoomMemberEvent.TYPE:
    #                 if s.content["membership"] == Membership.JOIN:
    #                     destinations.add(
    #                         self.hs.parse_userid(s.state_key).domain
    #                     )
    #         except:
    #             logger.warn(
    #                 "Failed to get destination from event %s", s.event_id
    #             )
    #
    #     event.destinations = list(destinations)
    #
    #     yield self.notifier.on_new_room_event(event, extra_users=extra_users)
    #
    #     federation_handler = self.hs.get_handlers().federation_handler
    #     yield federation_handler.handle_new_event(event, snapshot)
