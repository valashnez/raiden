# -*- coding: utf8 -*-
from ethereum import slogging

from raiden.utils import isaddress
from raiden.blockchain.net_contract import NettingChannelContract

log = slogging.getLogger('raiden.rpc.client')  # pylint: disable=invalid-name


class BlockChainService(object):
    """ Exposes the blockchain's state through JSON-RPC. """
    # pylint: disable=no-self-use

    def next_block(self):
        raise NotImplementedError

    @property
    def block_number(self):
        raise NotImplementedError

    def asset_addresses(self):
        raise NotImplementedError

    def contracts_by_asset_participant(self, asset_address, participant_address):  # pylint: disable=invalid-name
        raise NotImplementedError

    def new_channel_contract(self, asset_address):
        raise NotImplementedError

    def new_channel(self, asset_address, peer1, peer2):
        raise NotImplementedError


class BlockChainServiceMock(object):
    """ Mock implementation of BlockChainService that doesn't do JSON-RPC and
    doesn't require a running node.

    A mock block chain, the user can assume that this mock represents
    up-to-date information.

    The actions that the user can perform on the blockchain are:

        - Transfer money to a contract/channel to create it
        - Create a new channel, by executing an exiting contract

        - Call a method in an existing channel (close and settle)
        - List existing  channels for a given address (?)

    Note:
        Useful for testing
    """

    # Note: all these methods need to be "atomic" because the mock is going to
    # be used by multiple clients. Not using blocking functions should be
    # sufficient

    def __init__(self):
        self.block_number = 0
        self.asset_hashchannel = dict()

    def next_block(self):
        """ Equivalent to the mining of a new block.

        Note:
            This method does not create any representation of the new block, it
            just increases current block number. This is necessary since the
            channel contract needs the current block number to decide if the
            closing of a channel can be closed or not.
        """
        self.block_number += 1

    def new_channel_contract(self, asset_address):
        """ The equivalent of creating a new contract that will manage channels
        in the blockchain.

        Raises:
            ValueError: If asset_address is not a valid address or is already registered.
        """
        if not isaddress(asset_address):
            raise ValueError('The asset must be a valid address')

        if asset_address in self.asset_hashchannel:
            raise ValueError('This asset already has a registered contract')

        self.asset_hashchannel[asset_address] = dict()

    def new_channel(self, asset_address, peer1, peer2):
        """ Creates a new channel between peer1 and peer2.

        Raises:
            ValueError: If peer1 or peer2 is not a valid address.
        """
        if not isaddress(peer1):
            raise ValueError('The pee1 must be a valid address')

        if not isaddress(peer2):
            raise ValueError('The peer2 must be a valid address')

        channel = NettingChannelContract(asset_address, peer1, peer2)

        hash_ = ''.join(sorted((peer1, peer2)))  # fixme replace by sha3

        hash_channel = self.asset_hashchannel[asset_address]
        hash_channel[hash_] = channel

        return channel

    @property
    def asset_addresses(self):
        """ Return all assets addresses that have a contract. """
        return self.asset_hashchannel.keys()

    def contracts_by_asset(self, asset_address):
        """ Return all channels that exists for a given asset. """
        # this information is required for building the network graph
        hash_channel = self.asset_hashchannel[asset_address]
        return hash_channel.values()

    def contracts_by_asset_participant(self, asset_address, participant_address):
        """ Return all channels for a given asset that `participant_address` is
        a participant.
        """
        manager = self.asset_hashchannel[asset_address]

        return [
            channel
            for channel in manager.values()
            if participant_address in channel.participants
        ]