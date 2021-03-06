import asyncio
import binascii
import random

import kademlia
from apistar import http, Route, App
from apistar.http import Response
from bytestring_splitter import VariableLengthBytestring
from constant_sorrow import constants
from kademlia.crawling import NodeSpiderCrawl
from kademlia.network import Server
from kademlia.utils import digest
from typing import ClassVar

from hendrix.experience import crosstown_traffic
from nucypher.crypto.kits import UmbralMessageKit
from nucypher.crypto.powers import SigningPower, TLSHostingPower
from nucypher.keystore.keypairs import HostingKeypair
from nucypher.keystore.threading import ThreadedSession
from nucypher.network.protocols import NucypherSeedOnlyProtocol, NucypherHashProtocol, InterfaceInfo
from nucypher.network.storage import SeedOnlyStorage
from umbral import pre
from umbral.fragments import KFrag


class NucypherDHTServer(Server):
    protocol_class = NucypherHashProtocol
    capabilities = ()
    digests_set = 0

    def __init__(self, node_storage, treasure_map_storage, federated_only=False, id=None, *args, **kwargs):
        super().__init__(ksize=20, alpha=3, id=None, storage=None)
        self.node = kademlia.node.Node(id=id or digest(
            random.getrandbits(255)))  # TODO: Assume that this can be attacked to get closer to desired kFrags.

        # What an awful monkey patch.  Part of the journey of deprecating the DHT.  # TODO: 340
        self.node._node_storage = node_storage
        self.node._treasure_maps = treasure_map_storage

        self.node.federated_only = federated_only

    async def set_digest(self, dkey, value):
        """
        Set the given SHA1 digest key (bytes) to the given value in the network.

        Returns True if a digest was in fact set.
        """
        node = self.node_class(dkey)

        nearest = self.protocol.router.findNeighbors(node)
        if len(nearest) == 0:
            self.log.warning("There are no known neighbors to set key %s" % dkey.hex())
            return False

        spider = NodeSpiderCrawl(self.protocol, node, nearest, self.ksize, self.alpha)
        nodes = await spider.find()

        self.log.info("setting '%s' on %s" % (dkey.hex(), list(map(str, nodes))))

        # if this node is close too, then store here as well
        if self.node.distanceTo(node) < max([n.distanceTo(node) for n in nodes]):
            self.storage[dkey] = value
        ds = []
        for n in nodes:
            _disposition, value_was_set = await self.protocol.callStore(n, dkey, value)
            if value_was_set:
                self.digests_set += 1
            ds.append(value_was_set)
        # return true only if at least one store call succeeded
        return any(ds)

    def get_now(self, key):
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(self.get(bytes(key)))

    def set_now(self, key, value):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            # Terrible.  We cant' get off the DHT soon enough.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(self.set(key, value))

    async def set(self, key, value):
        """
        Set the given string key to the given value in the network.
        """
        self.log.debug("setting '%s' = '%s' on network" % (key, value))
        key = digest(bytes(key))
        return await self.set_digest(key, value)


class NucypherSeedOnlyDHTServer(NucypherDHTServer):
    protocol_class = NucypherSeedOnlyProtocol

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.storage = SeedOnlyStorage()


class ProxyRESTServer:

    def __init__(self,
                 rest_host=None,
                 rest_port=None,
                 db_name=None,
                 tls_private_key=None,
                 tls_curve=None,
                 certificate=None,
                 *args, ** kwargs) -> None:
        self.rest_interface = InterfaceInfo(host=rest_host, port=rest_port)
        self.db_name = db_name
        self._rest_app = None
        tls_hosting_keypair = HostingKeypair(common_name=self.checksum_public_address,
                                             private_key=tls_private_key,
                                             curve=tls_curve,
                                             host=rest_host,
                                             certificate=certificate)
        tls_hosting_power = TLSHostingPower(keypair=tls_hosting_keypair)
        self._crypto_power.consume_power_up(tls_hosting_power)

    def public_material(self, power_class: ClassVar):
        """Implemented on Ursula"""
        raise NotImplementedError

    def canonical_public_address(self):
        """Implemented on Ursula"""
        raise NotImplementedError

    def stamp(self, *args, **kwargs):
        """Implemented on Ursula"""
        raise NotImplementedError

    def attach_rest_server(self):

        routes = [
            Route('/kFrag/{id_as_hex}',
                  'POST',
                  self.set_policy),
            Route('/kFrag/{id_as_hex}/reencrypt',
                  'POST',
                  self.reencrypt_via_rest),
            Route('/public_information', 'GET',
                  self.public_information),
            Route('/node_metadata', 'GET',
                  self.all_known_nodes),
            Route('/node_metadata', 'POST',
                  self.node_metadata_exchange),
            Route('/consider_arrangement',
                  'POST',
                  self.consider_arrangement),
            Route('/treasure_map/{treasure_map_id}',
                  'GET',
                  self.provide_treasure_map),
            Route('/treasure_map/{treasure_map_id}',
                  'POST',
                  self.receive_treasure_map),
        ]

        self._rest_app = App(routes=routes)
        self.start_datastore(self.db_name)

    def start_datastore(self, db_name):
        if not db_name:
            raise TypeError("In order to start a datastore, you need to supply a db_name.")

        from nucypher.keystore import keystore
        from nucypher.keystore.db import Base
        from sqlalchemy.engine import create_engine

        self.log.info("Starting datastore {}".format(db_name))
        engine = create_engine('sqlite:///{}'.format(db_name))
        Base.metadata.create_all(engine)
        self.datastore = keystore.KeyStore(engine)
        self.db_engine = engine

    def rest_url(self):
        return "{}:{}".format(self.rest_interface.host, self.rest_interface.port)

    #####################################
    # Actual REST Endpoints and utilities
    #####################################

    def public_information(self):
        """
        REST endpoint for public keys and address..
        """
        headers = {'Content-Type': 'application/octet-stream'}
        response = Response(
            content=bytes(self),
            headers=headers)

        return response

    def all_known_nodes(self, request: http.Request):
        headers = {'Content-Type': 'application/octet-stream'}
        ursulas_as_bytes = bytes().join(bytes(n) for n in self._known_nodes.values())
        ursulas_as_bytes += bytes(self)
        signature = self.stamp(ursulas_as_bytes)
        return Response(bytes(signature) + ursulas_as_bytes, headers=headers)

    def node_metadata_exchange(self, request: http.Request, query_params: http.QueryParams):

        nodes = self.batch_from_bytes(request.body, federated_only=self.federated_only)
        # TODO: This logic is basically repeated in learn_from_teacher_node.  Let's find a better way.
        for node in nodes:

            if node.checksum_public_address in self._known_nodes:
                continue  # TODO: 168 Check version and update if required.

            @crosstown_traffic()
            def learn_about_announced_nodes():
                try:
                    node.verify_node(self.network_middleware, accept_federated_only=self.federated_only)
                except node.SuspiciousActivity:
                    # TODO: Account for possibility that stamp, rather than interface, was bad.
                    message = "Suspicious Activity: Discovered node with bad signature: {}.  " \
                              " Announced via REST."  # TODO: Include data about caller?
                    self.log.warning(message)

                self.log.info("Previously unknown node: {}".format(node.checksum_public_address))
                self.remember_node(node)

        # TODO: What's the right status code here?  202?  Different if we already knew about the node?
        return self.all_known_nodes(request)

    def consider_arrangement(self, request: http.Request):
        from nucypher.policy.models import Arrangement
        arrangement = Arrangement.from_bytes(request.body)

        with ThreadedSession(self.db_engine) as session:
            new_policyarrangement = self.datastore.add_policy_arrangement(
                arrangement.expiration.datetime(),
                id=arrangement.id.hex().encode(),
                alice_pubkey_sig=arrangement.alice.stamp,
                session=session,
            )
        # TODO: Make the rest of this logic actually work - do something here
        # to decide if this Arrangement is worth accepting.

        headers = {'Content-Type': 'application/octet-stream'}
        # TODO: Make this a legit response #234.
        return Response(b"This will eventually be an actual acceptance of the arrangement.", headers=headers)

    def set_policy(self, id_as_hex, request: http.Request):
        """
        REST endpoint for setting a kFrag.
        TODO: Instead of taking a Request, use the apistar typing system to type
            a payload and validate / split it.
        TODO: Validate that the kfrag being saved is pursuant to an approved
            Policy (see #121).
        """
        policy_message_kit = UmbralMessageKit.from_bytes(request.body)

        alice = self._alice_class.from_public_keys({SigningPower: policy_message_kit.sender_pubkey_sig})

        try:
            cleartext = self.verify_from(alice, policy_message_kit, decrypt=True)
        except self.InvalidSignature:
            # TODO: What do we do if the Policy isn't signed properly?
            pass

        kfrag = KFrag.from_bytes(cleartext)

        with ThreadedSession(self.db_engine) as session:
            self.datastore.attach_kfrag_to_saved_arrangement(
                alice,
                id_as_hex,
                kfrag,
                session=session)

        return  # TODO: Return A 200, with whatever policy metadata.

    def reencrypt_via_rest(self, id_as_hex, request: http.Request):
        from nucypher.policy.models import WorkOrder  # Avoid circular import
        id = binascii.unhexlify(id_as_hex)
        work_order = WorkOrder.from_rest_payload(id, request.body)
        self.log.info("Work Order from {}, signed {}".format(work_order.bob, work_order.receipt_signature))
        with ThreadedSession(self.db_engine) as session:
            kfrag_bytes = self.datastore.get_policy_arrangement(id.hex().encode(),
                                                                session=session).k_frag  # Careful!  :-)
        # TODO: Push this to a lower level.
        kfrag = KFrag.from_bytes(kfrag_bytes)
        cfrag_byte_stream = b""

        for capsule in work_order.capsules:
            # TODO: Sign the result of this.  See #141.
            cfrag = pre.reencrypt(kfrag, capsule)
            self.log.info("Re-encrypting for Capsule {}, made CFrag {}.".format(capsule, cfrag))
            cfrag_byte_stream += VariableLengthBytestring(cfrag)

        # TODO: Put this in Ursula's datastore
        self._work_orders.append(work_order)

        headers = {'Content-Type': 'application/octet-stream'}

        return Response(content=cfrag_byte_stream, headers=headers)

    def provide_treasure_map(self, treasure_map_id):
        headers = {'Content-Type': 'application/octet-stream'}

        try:
            treasure_map = self.treasure_maps[digest(treasure_map_id)]
            response = Response(content=bytes(treasure_map), headers=headers)
            self.log.info("{} providing TreasureMap {}".format(self, treasure_map_id))
        except KeyError:
            self.log.info("{} doesn't have requested TreasureMap {}".format(self, treasure_map_id))
            response = Response("No Treasure Map with ID {}".format(treasure_map_id),
                                status_code=404, headers=headers)

        return response

    def receive_treasure_map(self, treasure_map_id, request: http.Request):
        from nucypher.policy.models import TreasureMap

        try:
            treasure_map = TreasureMap.from_bytes(
                bytes_representation=request.body,
                verify=True)
        except TreasureMap.InvalidSignature:
            do_store = False
        else:
            do_store = treasure_map.public_id() == treasure_map_id

        if do_store:
            self.log.info("{} storing TreasureMap {}".format(self, treasure_map_id))
            self.dht_server.set_now(binascii.unhexlify(treasure_map_id),
                                    constants.BYTESTRING_IS_TREASURE_MAP + bytes(treasure_map))

            # TODO 341 - what if we already have this TreasureMap?
            self.treasure_maps[digest(treasure_map_id)] = treasure_map
            return Response(content=bytes(treasure_map), status_code=202)
        else:
            # TODO: Make this a proper 500 or whatever.
            self.log.info("Bad TreasureMap ID; not storing {}".format(treasure_map_id))
            assert False

    def get_deployer(self):
        deployer = self._crypto_power.power_ups(TLSHostingPower).get_deployer(rest_app=self._rest_app,
                                                                              port=self.rest_interface.port)
        return deployer
