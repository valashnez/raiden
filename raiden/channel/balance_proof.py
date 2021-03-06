# -*- coding: utf-8 -*-
import logging
from collections import namedtuple
from itertools import chain

from ethereum import slogging

from raiden.messages import (
    DirectTransfer,
    LockedTransfer,
)
from raiden.mtree import Merkletree
from raiden.utils import sha3, pex
from raiden.exceptions import (
    InvalidLocksRoot,
)

log = slogging.getLogger(__name__)  # pylint: disable=invalid-name


# A lock and its computed hash, this namedtuple is used to keep the
# `sha3(lock.as_bytes)` cached since this value is used to construct the
# merkletree
PendingLock = namedtuple('PendingLock', ('lock', 'lockhashed'))

# The lock and the secret to unlock it, this is all the data required to
# construct an unlock proof. The proof is not calculated because we only need
# it when the contract is closed.
UnlockPartialProof = namedtuple('UnlockPartialProof', ('lock', 'lockhashed', 'secret'))

# The proof that can be used to unlock a secret with a smart contract
UnlockProof = namedtuple('UnlockProof', ('merkle_proof', 'lock_encoded', 'secret'))


class BalanceProof(object):
    """ Saves the state required to settle a netting contract. """

    def __init__(self):
        # locks that we are mediating but the secret is unknown
        self.hashlocks_to_pendinglocks = dict()

        # locks for which we know the secret but our partner hasn't updated
        # their state yet
        self.hashlocks_to_unclaimedlocks = dict()

        # locks for which we know the secret and the partner has updated their
        # state but we don't have an up-to-date transfer to use as a proof
        self.hashlocks_to_unlockedlocks = dict()

        # the latest known transfer with a correct locksroot that can be used
        # as a proof
        self.transfer = None

    def unclaimed_merkletree(self):
        alllocks = chain(
            self.hashlocks_to_pendinglocks.values(),
            self.hashlocks_to_unclaimedlocks.values()
        )
        return [lock.lockhashed for lock in alllocks]

    def merkleroot_for_unclaimed(self):
        alllocks = chain(
            self.hashlocks_to_pendinglocks.values(),
            self.hashlocks_to_unclaimedlocks.values()
        )

        tree = Merkletree(lock.lockhashed for lock in alllocks)
        return tree.merkleroot

    def is_pending(self, hashlock):
        """ True if a secret is not known for the given `hashlock`. """
        return hashlock in self.hashlocks_to_pendinglocks

    def is_unclaimed(self, hashlock):
        """ True if a secret is known but we didnt claim it yet.

        A lock is not claimed until the partner sends the secret back.
        """
        return (
            hashlock in self.hashlocks_to_pendinglocks or
            hashlock in self.hashlocks_to_unclaimedlocks
        )

    def is_known(self, hashlock):
        """ True if a lock with the given hashlock was registered before. """
        return (
            hashlock in self.hashlocks_to_pendinglocks or
            hashlock in self.hashlocks_to_unclaimedlocks or
            hashlock in self.hashlocks_to_unlockedlocks
        )

    def locked(self):
        alllocks = chain(
            self.hashlocks_to_pendinglocks.values(),
            self.hashlocks_to_unclaimedlocks.values(),
            # self.hashlocks_to_unlockedlocks.values()
        )

        return sum(
            lock.lock.amount
            for lock in alllocks
        )

    def register_locked_transfer(self, locked_transfer):
        if not isinstance(locked_transfer, LockedTransfer):
            raise ValueError('transfer must be a LockedTransfer')

        lock = locked_transfer.lock
        lockhashed = sha3(lock.as_bytes)

        if self.is_known(lock.hashlock):
            raise ValueError('hashlock is already registered')

        leafs = self.unclaimed_merkletree()
        leafs.append(lockhashed)
        new_locksroot = Merkletree(leafs).merkleroot

        if locked_transfer.locksroot != new_locksroot:
            raise ValueError(
                'locksroot mismatch expected:{} got:{}'.format(
                    pex(new_locksroot),
                    pex(locked_transfer.locksroot),
                )
            )

        self.hashlocks_to_pendinglocks[lock.hashlock] = PendingLock(lock, lockhashed)
        self.transfer = locked_transfer
        self.hashlocks_to_unlockedlocks = dict()

    def register_direct_transfer(self, direct_transfer):
        if not isinstance(direct_transfer, DirectTransfer):
            raise ValueError('transfer must be a DirectTransfer')

        unclaimed_locksroot = self.merkleroot_for_unclaimed()

        if direct_transfer.locksroot != unclaimed_locksroot:
            raise InvalidLocksRoot(unclaimed_locksroot, direct_transfer.locksroot)

        self.transfer = direct_transfer
        self.hashlocks_to_unlockedlocks = dict()

    def get_lock_by_hashlock(self, hashlock):
        """ Return the corresponding lock for the given `hashlock`. """
        pendinglock = self.hashlocks_to_pendinglocks.get(hashlock)

        if pendinglock:
            return pendinglock.lock

        pendinglock = self.hashlocks_to_unclaimedlocks.get(hashlock)

        if pendinglock:
            return pendinglock.lock

        unlockedlock = self.hashlocks_to_unlockedlocks[hashlock]
        return unlockedlock.lock

    def register_secret(self, secret, hashlock=None):
        if hashlock is None:
            hashlock = sha3(secret)

        if not self.is_known(hashlock):
            raise ValueError('secret does not correspond to any known lock.')

        if self.is_pending(hashlock):
            pendinglock = self.hashlocks_to_pendinglocks[hashlock]
            del self.hashlocks_to_pendinglocks[hashlock]

            self.hashlocks_to_unclaimedlocks[hashlock] = UnlockPartialProof(
                pendinglock.lock,
                pendinglock.lockhashed,
                secret,
            )
        elif log.isEnabledFor(logging.DEBUG):
            log.debug(
                'SECRET REGISTERED MORE THAN ONCE hashlock:%s',
                pex(hashlock),
            )

    def release_lock_by_secret(self, secret, hashlock=None):
        if hashlock is None:
            hashlock = sha3(secret)

        if self.is_pending(hashlock):
            pendinglock = self.hashlocks_to_pendinglocks[hashlock]
            del self.hashlocks_to_pendinglocks[hashlock]

            self.hashlocks_to_unlockedlocks[hashlock] = UnlockPartialProof(
                pendinglock.lock,
                pendinglock.lockhashed,
                secret,
            )

            return pendinglock.lock

        elif self.is_unclaimed(hashlock):
            unclaimedlock = self.hashlocks_to_unclaimedlocks[hashlock]
            del self.hashlocks_to_unclaimedlocks[hashlock]

            self.hashlocks_to_unlockedlocks[hashlock] = unclaimedlock

            return unclaimedlock.lock

        raise ValueError('Unknown hashlock')

    def get_known_unlocks(self):
        """ Generate unlocking proofs for the known secrets. """
        allpartialproof = chain(
            self.hashlocks_to_unclaimedlocks.itervalues(),
            self.hashlocks_to_unlockedlocks.itervalues(),
        )

        tree = self.generate_merkle_tree()

        return [
            self.compute_proof_for_lock(
                partialproof.secret,
                partialproof.lock,
                tree,
            )
            for partialproof in allpartialproof
        ]

    def compute_proof_for_lock(self, secret, lock, tree=None):
        if tree is None:
            tree = self.generate_merkle_tree()

        # forcing bytes because ethereum.abi doesnt work with bytearray
        lock_encoded = bytes(lock.as_bytes)
        lock_hash = sha3(lock_encoded)

        merkle_proof = tree.make_proof(lock_hash)

        return UnlockProof(
            merkle_proof,
            lock_encoded,
            secret,
        )

    # generate a Merkle tree for the known locks
    def generate_merkle_tree(self):
        alllocks = chain(
            self.hashlocks_to_pendinglocks.values(),
            self.hashlocks_to_unclaimedlocks.values(),
            self.hashlocks_to_unlockedlocks.values()
        )
        return Merkletree(lock.lockhashed for lock in alllocks)

    def __eq__(self, other):
        if isinstance(other, BalanceProof):
            return (
                self.hashlocks_to_pendinglocks == other.hashlocks_to_pendinglocks and
                self.hashlocks_to_unclaimedlocks == other.hashlocks_to_unclaimedlocks and
                self.hashlocks_to_unlockedlocks == other.hashlocks_to_unlockedlocks and
                self.transfer == other.transfer
            )
        return False

    def __ne__(self, other):
        return not self.__eq__(other)
