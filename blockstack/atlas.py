#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Blockstack
    ~~~~~
    copyright: (c) 2014-2015 by Halfmoon Labs, Inc.
    copyright: (c) 2016 by Blockstack.org

    This file is part of Blockstack

    Blockstack is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Blockstack is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with Blockstack. If not, see <http://www.gnu.org/licenses/>.
"""

import os
import sys
import time
import sqlite3
import urllib2
import simplejson
import threading
import random
import struct
import base64
import shutil
import traceback
import copy
import binascii
import StringIO
import hashlib
import errno
import socket

import blockstack_zones
import virtualchain

from blockstack_client.config import url_to_host_port, semver_match, semver_newer

log = virtualchain.get_logger("blockstack-server")

from lib.config import *
from lib.storage import *
from lib import get_db_state

from pybloom_live import BloomFilter, ScalableBloomFilter

MIN_ATLAS_VERSION = "0.14.0"

PEER_LIFETIME_INTERVAL = 3600  # 1 hour
PEER_PING_INTERVAL = 60        # 1 minute
PEER_MAX_AGE = 2678400         # 1 month
PEER_CLEAN_INTERVAL = 3600     # 1 hour
PEER_MAX_DB = 65536            # maximum number of peers in the peer db
MIN_PEER_HEALTH = 0.5  # minimum peer health before we forget about it

PEER_PING_TIMEOUT = 3   # number of seconds for a ping to take
PEER_INV_TIMEOUT  = 10  # number of seconds for an inv to take
PEER_NEIGHBORS_TIMEOUT = 10 # number of seconds for a neighbors query to take
PEER_ZONEFILES_TIMEOUT = 30 # number of seconds for a zonefile query to take
PEER_PUSH_ZONEFILES_TIMEOUT = 10

NUM_NEIGHBORS = 80     # number of neighbors a peer can report

ZONEFILE_INV = None      # this atlas peer's current zonefile inventory
NUM_ZONEFILES = 0      # cache-coherent count of the number of zonefiles present

MAX_QUEUED_ZONEFILES = 1000     # maximum number of queued zonefiles

if os.environ.get("BLOCKSTACK_ATLAS_PEER_LIFETIME") is not None:
    PEER_LIFETIME_INTERVAL = int(os.environ.get("BLOCKSTACK_ATLAS_PEER_LIFETIME"))

if os.environ.get("BLOCKSTACK_ATLAS_PEER_PING_INTERVAL") is not None:
    PEER_PING_INTERVAL = int(os.environ.get("BLOCKSTACK_ATLAS_PEER_PING_INTERVAL"))

if os.environ.get("BLOCKSTACK_ATLAS_MIN_PEER_HEALTH") is not None:
    MIN_PEER_HEALTH = float(os.environ.get("BLOCKSTACK_ATLAS_MIN_PEER_HEALTH"))

if os.environ.get("BLOCKSTACK_ATLAS_NUM_NEIGHBORS") is not None:
    NUM_NEIGHBORS = int(os.environ.get("BLOCKSTACK_ATLAS_NUM_NEIGHBORS"))

if os.environ.get("BLOCKSTACK_ATLAS_MAX_NEIGHBORS") is not None:
    NUM_NEIGHBORS = int(os.environ.get("BLOCKSTACK_ATLAS_MAX_NEIGHBORS"))

if os.environ.get("BLOCKSTACK_TEST", None) == "1" and os.environ.get("BLOCKSTACK_ATLAS_NETWORK_SIMULATION", None) == "1" and os.environ.get("BLOCKSTACK_ATLAS_NETWORK_SIMULATION_PEER", None) == "1":
    # subordinate atlas peer in the simulator.
    # use test client
    from blockstack_integration_tests import AtlasRPCTestClient as BlockstackRPCClient
    from blockstack_integration_tests import time_now, time_sleep, atlas_max_neighbors, \
            atlas_peer_lifetime_interval, atlas_peer_ping_interval, atlas_peer_max_age, \
            atlas_peer_clean_interval, atlas_ping_timeout, atlas_inv_timeout, atlas_neighbors_timeout, \
            atlas_zonefiles_timeout, atlas_push_zonefiles_timeout

else:
    # production
    from blockstack_client import BlockstackRPCClient
    
    def time_now():
        return time.time()

    def time_sleep(hostport, procname, value):
        return time.sleep(value)
    
    def atlas_max_neighbors():
        return NUM_NEIGHBORS

    def atlas_peer_lifetime_interval():
        return PEER_LIFETIME_INTERVAL

    def atlas_peer_ping_interval():
        return PEER_PING_INTERVAL

    def atlas_peer_max_age():
        return PEER_MAX_AGE

    def atlas_peer_clean_interval():
        return PEER_CLEAN_INTERVAL

    def atlas_ping_timeout():
        return PEER_PING_TIMEOUT

    def atlas_inv_timeout():
        return PEER_INV_TIMEOUT

    def atlas_neighbors_timeout():
        return PEER_NEIGHBORS_TIMEOUT

    def atlas_zonefiles_timeout():
        return PEER_ZONEFILES_TIMEOUT

    def atlas_push_zonefiles_timeout():
        return PEER_PUSH_ZONEFILES_TIMEOUT


ATLASDB_SQL = """
CREATE TABLE zonefiles( inv_index INTEGER PRIMARY KEY AUTOINCREMENT,
                        zonefile_hash TEXT NOT NULL,
                        present INTEGER NOT NULL,
                        tried_storage INTEGER NOT NULL,
                        block_height INTEGER NOT NULL );

CREATE TABLE peers( peer_index INTEGER PRIMARY KEY AUTOINCREMENT,
                    peer_slot INTEGER NOT NULL,
                    peer_hostport STRING UNIQUE NOT NULL,
                    discovery_time INTEGER NOT NULL );
"""

PEER_TABLE = {}        # map peer host:port (NOT url) to peer information
                       # each element is {'time': [(responded, timestamp)...], 'zonefile_inv': ...}
                       # 'zonefile_inv' is a *bitwise big-endian* bit string where bit i is set if the zonefile in the ith NAME_UPDATE transaction has been stored by us (i.e. "is present")
                       # for example, if 'zonefile_inv' is 10110001, then the 0th, 2nd, 3rd, and 7th NAME_UPDATEs' zonefiles have been stored by us
                       # (note that we allow for the possibility of duplicate zonefiles, but this is a rare occurance and we keep track of it in the DB to avoid duplicate transfers)

PEER_QUEUE = []        # list of peers (host:port) to begin talking to, discovered via the Atlas RPC interface
ZONEFILE_QUEUE = []    # list of {zonefile_hash: zonefile} dicts to push out to other Atlas nodes (i.e. received from clients)

PEER_TABLE_LOCK = threading.Lock()
PEER_QUEUE_LOCK = threading.Lock()
PEER_TABLE_LOCK_HOLDER = None
ZONEFILE_QUEUE_LOCK = threading.Lock()
DB_LOCK = threading.Lock()

def atlas_peer_table_lock():
    """
    Lock the global health info table.
    Return the table.
    """
    global PEER_TABLE_LOCK, PEER_TABLE, PEER_TABLE_LOCK_HOLDER

    if PEER_TABLE_LOCK_HOLDER is not None:
        assert PEER_TABLE_LOCK_HOLDER != threading.current_thread(), "DEADLOCK"
        # log.warning("\n\nPossible contention: lock from %s (but held by %s)\n\n" % (threading.current_thread(), PEER_TABLE_LOCK_HOLDER))

    PEER_TABLE_LOCK.acquire()
    PEER_TABLE_LOCK_HOLDER = threading.current_thread()
    return PEER_TABLE


def atlas_peer_table_is_locked():
    """
    Is the peer table locked?
    """
    global PEER_TABLE_LOCK_HOLDER
    return (PEER_TABLE_LOCK_HOLDER is not None)


def atlas_peer_table_is_locked_by_me():
    """
    Is the peer table locked by the calling thread?
    """
    global PEER_TABLE_LOCK_HOLDER
    return (PEER_TABLE_LOCK_HOLDER == threading.current_thread())


def atlas_peer_table_unlock():
    """
    Unlock the global health info table.
    """
    global PEER_TABLE_LOCK, PEER_TABLE_LOCK_HOLDER
    PEER_TABLE_LOCK_HOLDER = None
    PEER_TABLE_LOCK.release()
    return


def atlas_peer_queue_lock():
    """
    Lock the global peer queue
    return the queue
    """
    global PEER_QUEUE_LOCK, PEER_QUEUE
    PEER_QUEUE_LOCK.acquire()
    return PEER_QUEUE


def atlas_peer_queue_unlock():
    """
    Unlock the global peer queue
    """
    global PEER_QUEUE_LOCK
    PEER_QUEUE_LOCK.release()


def atlas_zonefile_queue_lock():
    """
    Lock the global zonefile queue
    return the queue
    """
    global ZONEFILE_QUEUE_LOCK
    ZONEFILE_QUEUE_LOCK.acquire()
    return ZONEFILE_QUEUE


def atlas_zonefile_queue_unlock():
    """
    Unlock the global zonefile queue
    """
    global ZONEFILE_QUEUE_LOCK
    ZONEFILE_QUEUE_LOCK.release()


def atlas_max_new_peers( max_neighbors ):
    """
    Maximum size of the new peers list
    """
    max_new_peers = min(max_neighbors * 10, PEER_MAX_DB)
    return max_new_peers


def atlas_inventory_to_string( inv ):
    """
    Inventory to string (bitwise big-endian)
    """
    ret = ""
    for i in xrange(0, len(inv)):
        for j in xrange(0, 8):
            bit_index = 1 << (7 - j)
            val = (ord(inv[i]) & bit_index)
            if val != 0:
                ret += "1"
            else:
                ret += "0"

    return ret


def atlas_inventory_flip_zonefile_bits( inv_vec, bit_indexes, operation ):
    """
    Given a list of bit indexes (bit_indexes), set or clear the
    appropriate bits in the inventory vector (inv_vec).
    If the bit index is beyond the length of inv_vec, 
    then expand inv_vec to accomodate it.

    If operation is True, then set the bits.
    If operation is False, then clear the bits

    Return the new inv_vec
    """
    inv_list = list(inv_vec)

    max_byte_index = max(bit_indexes) / 8 + 1
    if len(inv_list) <= max_byte_index:
        inv_list += ['\0'] * (max_byte_index - len(inv_list))

    for bit_index in bit_indexes:
        byte_index = bit_index / 8
        bit_index = 7 - (bit_index % 8)
        
        zfbits = ord(inv_list[byte_index])

        if operation:
            zfbits = zfbits | (1 << bit_index)
        else:
            zfbits = zfbits & ~(1 << bit_index)

        inv_list[byte_index] = chr(zfbits)

    return "".join(inv_list)


def atlas_inventory_set_zonefile_bits( inv_vec, bit_indexes ):
    """
    Set bits in a zonefile inventory vector.
    """
    return atlas_inventory_flip_zonefile_bits( inv_vec, bit_indexes, True )


def atlas_inventory_clear_zonefile_bits( inv_vec, bit_indexes ):
    """
    Clear bits in a zonefile inventory vector
    """
    return atlas_inventory_flip_zonefile_bits( inv_vec, bit_indexes, False )


def atlas_inventory_test_zonefile_bits( inv_vec, bit_indexes ):
    """
    Given a list of bit indexes (bit_indexes), determine whether or not 
    they are set.

    Return True if all are set
    Return False if not
    """
    inv_list = list(inv_vec)

    max_byte_index = max(bit_indexes) / 8 + 1
    if len(inv_list) <= max_byte_index:
        inv_list += ['\0'] * (max_byte_index - len(inv_list))

    ret = True
    for bit_index in bit_indexes:
        byte_index = bit_index / 8
        bit_index = 7 - (bit_index % 8)
        
        zfbits = ord(inv_list[byte_index])
        ret = (ret and ((zfbits & (1 << bit_index)) != 0))

    return ret


def atlasdb_row_factory( cursor, row ):
    """
    row factory
    * convert known booleans to booleans
    """
    d = {}
    for idx, col in enumerate( cursor.description ):
        if col[0] in ["present", "tried_storage"]:
            if row[idx] == 0:
                d[col[0]] = False
            elif row[idx] == 1:
                d[col[0]] = True
            else:
                raise Exception("Invalid value for '%s': %s" % (col[0], row[idx]))

        else:
            d[col[0]] = row[idx]

    return d


def atlasdb_path( impl=None ):
    """
    Get the path to the atlas DB
    """
    working_dir = virtualchain.get_working_dir(impl=impl)
    return os.path.join(working_dir, "atlas.db")


def atlasdb_query_execute( cur, query, values ):
    """
    Execute a query.  If it fails, exit.

    DO NOT CALL THIS DIRECTLY.
    """

    # under heavy contention, this can cause timeouts (which is unacceptable)
    # serialize access to the db juts to be safe
    
    global DB_LOCK

    try:
        DB_LOCK.acquire()
        ret = cur.execute( query, values )
        DB_LOCK.release()
        return ret
    except Exception, e:
        log.exception(e)
        log.error("FATAL: failed to execute query (%s, %s)" % (query, values))
        log.error("\n" + "\n".join(traceback.format_stack()))
        os.abort()


def atlasdb_open( path ):
    """
    Open the atlas db.
    Return a connection.
    Return None if it doesn't exist
    """
    if not os.path.exists(path):
        log.debug("Atlas DB doesn't exist at %s" % path)
        return None

    con = sqlite3.connect( path, isolation_level=None )
    con.row_factory = atlasdb_row_factory
    return con


def atlasdb_add_zonefile_info( zonefile_hash, present, block_height, con=None, path=None ):
    """
    Add a zonefile to the database.
    Mark it as present or absent.
    Keep our in-RAM inventory vector up-to-date
    """
    global ZONEFILE_INV, NUM_ZONEFILES

    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "INSERT INTO zonefiles (zonefile_hash, present, tried_storage, block_height) VALUES (?,?,?,?);"
    args = (zonefile_hash, present, 0, block_height)

    cur = con.cursor()
    atlasdb_query_execute( cur, sql, args )
    con.commit()

    # keep in-RAM zonefile inv coherent
    zfbits = atlasdb_get_zonefile_bits( zonefile_hash, con=con, path=path )

    inv_vec = None
    if ZONEFILE_INV is None:
        inv_vec = ""
    else:
        inv_vec = ZONEFILE_INV[:]

    ZONEFILE_INV = atlas_inventory_flip_zonefile_bits( inv_vec, zfbits, present )

    # keep in-RAM zonefile count coherent
    NUM_ZONEFILES = atlasdb_zonefile_inv_length( con=con, path=path )

    if close:
        con.close()

    return True


def atlasdb_get_lastblock( con=None, path=None ):
    """
    Get the highest block height in the atlas db
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "SELECT MAX(block_height) FROM zonefiles;"
    args = ()

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    row = {}
    for r in res:
        row.update(r)
        break

    if close:
        con.close()

    return row['MAX(block_height)']



def atlasdb_get_zonefile( zonefile_hash, con=None, path=None ):
    """
    Look up all information on this zonefile.
    Returns {'zonefile_hash': ..., 'indexes': [...], etc}
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "SELECT * FROM zonefiles WHERE zonefile_hash = ?;"
    args = (zonefile_hash,)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    ret = {
        'zonefile_hash': zonefile_hash,
        'indexes': [],
        'block_heights': [],
        'present': False,
        'tried_storage': False
    }

    for zfinfo in res:
        ret['indexes'].append( zfinfo['inv_index'] )
        ret['block_heights'].append( zfinfo['block_height'] )
        ret['present'] = ret['present'] or zfinfo['present']
        ret['tried_storage'] = ret['tried_storage'] or zfinfo['tried_storage']

    if close:
        con.close()

    return ret


def atlasdb_set_zonefile_present( zonefile_hash, present, con=None, path=None ):
    """
    Mark a zonefile as present (i.e. we stored it).
    Keep our in-RAM zonefile inventory coherent.
    Return the previous state.
    """
    global ZONEFILE_INV

    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    if present:
        present = 1
    else:
        present = 0

    sql = "UPDATE zonefiles SET present = ? WHERE zonefile_hash = ?;"
    args = (present, zonefile_hash)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    zfbits = atlasdb_get_zonefile_bits( zonefile_hash, con=con, path=path )
    
    inv_vec = None
    if ZONEFILE_INV is None:
        inv_vec = ""
    else:
        inv_vec = ZONEFILE_INV[:]

    # did we know about this?
    was_present = atlas_inventory_test_zonefile_bits( inv_vec, zfbits )

    # keep our inventory vector coherent.
    ZONEFILE_INV = atlas_inventory_flip_zonefile_bits( inv_vec, zfbits, present )

    if close:
        con.close()

    return was_present


def atlasdb_set_zonefile_tried_storage( zonefile_hash, tried_storage, con=None, path=None ):
    """
    Make a note that we tried to get the zonefile from storage
    """
    global ZONEFILE_INV

    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    if tried_storage:
        tried_storage = 1
    else:
        tried_storage = 0

    sql = "UPDATE zonefiles SET tried_storage = ? WHERE zonefile_hash = ?;"
    args = (tried_storage, zonefile_hash)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )

    con.commit()
    if close:
        con.close()

    return True


def atlasdb_cache_zonefile_info( con=None, path=None ):
    """
    Load up and cache our zonefile inventory
    """
    global ZONEFILE_INV, NUM_ZONEFILES

    inv_len = atlasdb_zonefile_inv_length( con=con, path=path )
    inv = atlas_make_zonefile_inventory( 0, inv_len, con=con, path=path )

    ZONEFILE_INV = inv
    NUM_ZONEFILES = inv_len
    return inv


def atlasdb_get_zonefile_bits( zonefile_hash, con=None, path=None ):
    """
    What bit(s) in a zonefile inventory does a zonefile hash correspond to?
    Return their indexes in the bit field.
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "SELECT inv_index FROM zonefiles WHERE zonefile_hash = ?;"
    args = (zonefile_hash,)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    # NOTE: zero-indexed
    ret = []
    for r in res:
        ret.append( r['inv_index'] - 1 )

    if close:
        con.close()

    return ret


def atlasdb_zonefile_block_reset( con, db, drop_block, path=None ):
    """
    Drop all zonefile rows for a particular block.
    Used for queueing zonefiles at a particular start block
    (i.e. we don't want to deal with "partial" blocks represented
    in the db).
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "DELETE FROM zonefiles WHERE block_height = ?;"
    args = (drop_block,)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    if close:
        con.close()

    return True


def atlasdb_queue_zonefiles( con, db, start_block, zonefile_dir=None, validate=True ):
    """
    Queue all zonefile hashes in the BlockstackDB
    to the zonefile queue
    """
    # populate zonefile queue
    total = 0
    for block_height in xrange(start_block, db.lastblock+1, 1):

        zonefile_hashes = db.get_value_hashes_at( block_height )
        for zfhash in zonefile_hashes:
            zfhash = str(zfhash)
            present = is_zonefile_cached( zfhash, zonefile_dir=zonefile_dir, validate=validate ) 

            log.debug("Add %s at %s (present: %s)" % (zfhash, block_height, present) )
            atlasdb_add_zonefile_info( zfhash, present, block_height, con=con )
            total += 1

    log.debug("Queued %s zonefiles from %s-%s" % (total, start_block, db.lastblock))
    return True


def atlasdb_sync_zonefiles( db, start_block, zonefile_dir=None, validate=True, path=None, con=None ):
    """
    Synchronize atlas DB with name db
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    atlasdb_queue_zonefiles( con, db, start_block, zonefile_dir=zonefile_dir, validate=validate )

    if close:
        con.close()

    return True


def atlasdb_add_peer( peer_hostport, discovery_time=None, peer_table=None, con=None, path=None, ping_on_evict=True ):
    """
    Add a peer to the peer table.
    If the peer conflicts with another peer, ping it first, and only insert
    the new peer if the old peer is dead.

    Keep the in-RAM peer table cache-coherent as well.

    Return True if this peer was added to the table (or preserved)
    Return False if not
    """
    
    # bound the number of peers we add to PEER_MAX_DB
    assert len(peer_hostport) > 0

    sk = random.randint(0, 2**32)
    peer_host, peer_port = url_to_host_port( peer_hostport )

    assert len(peer_host) > 0 

    peer_slot = int( hashlib.sha256("%s%s" % (sk, peer_host)).hexdigest(), 16 ) % PEER_MAX_DB

    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    # if the peer is already present, then abort
    if peer_hostport in peer_table.keys():
        log.debug("%s already in the peer table" % peer_hostport)

        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        return True

    # connect to the db if we have to
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    if discovery_time is None:
        discovery_time = int(time.time())
   
    # not in the table yet.  See if we can evict someone
    if ping_on_evict:

        # don't hold the lock across network I/O
        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        sql = "SELECT peer_hostport FROM peers WHERE peer_slot = ?;"
        args = (peer_slot,)

        cur = con.cursor()
        res = atlasdb_query_execute( cur, sql, args )
        con.commit()

        old_hostports = []
        for row in res:
            old_hostport = res['peer_hostport']
            old_hostports.append( old_hostport )

        for old_hostport in old_hostports:
            # is this other peer still alive?
            res = atlas_peer_ping( old_hostport )
            if res:
                log.debug("Peer %s is still alive; will not replace" % (old_hostport))
                
                if close:
                    con.close()

                return False

        # re-acquire
        if locked:
            peer_table = atlas_peer_table_lock()

    log.debug("Add peer '%s' discovered at %s (slot %s)" % (peer_hostport, discovery_time, peer_slot))

    # peer is dead (or we don't care).  Can insert or update
    sql = "INSERT OR REPLACE INTO peers (peer_hostport, peer_slot, discovery_time) VALUES (?,?,?);"
    args = (peer_hostport, peer_slot, discovery_time)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    if close:
        con.close()

    # add to peer table as well
    atlas_init_peer_info( peer_table, peer_hostport, blacklisted=False, whitelisted=False )
    
    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return True


def atlasdb_remove_peer( peer_hostport, con=None, path=None, peer_table=None ):
    """
    Remove a peer from the peer db and (if given) peer table.
    """
  
    # connect to the db if we have to
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    log.debug("Delete peer '%s'" % peer_hostport)

    sql = "DELETE FROM peers WHERE peer_hostport = ?;"
    args = (peer_hostport,)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    if close:
        con.close()

    # remove from the peer table as well
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    if peer_table.has_key(peer_hostport):
        if not atlas_peer_is_whitelisted( peer_hostport, peer_table=peer_table ) and not atlas_peer_is_blacklisted( peer_hostport, peer_table=peer_table ):
            del peer_table[peer_hostport]

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return True


def atlasdb_num_peers( con=None, path=None ):
    """
    How many peers are there in the db?
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "SELECT MAX(peer_index) FROM peers;"
    args = ()

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    ret = []
    for row in res:
        tmp = {}
        tmp.update(row)
        ret.append(tmp)

    assert len(ret) == 1

    if close:
        con.close()

    return ret[0]['MAX(peer_index)']


def atlas_get_peer( peer_hostport, peer_table=None ):
    """
    Get the given peer's info
    """
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    ret = peer_table.get(peer_hostport, None)

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return ret


def atlasdb_get_random_peer( con=None, path=None ):
    """
    Select a peer from the db at random
    Return None if the table is empty
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    ret = {}

    num_peers = atlasdb_num_peers( con=con )
    if num_peers is None or num_peers == 0:
        # no peers
        ret['peer_hostport'] = None

    else:
        r = random.randint(1, num_peers)

        sql = "SELECT * FROM peers WHERE peer_index = ?;"
        args = (r,)

        cur = con.cursor()
        res = atlasdb_query_execute( cur, sql, args )
        con.commit()

        ret = {'peer_hostport': None}
        for row in res:
            ret.update( row )
            break

    if close:
        con.close()

    return ret['peer_hostport']


def atlasdb_get_old_peers( now, con=None, path=None ):
    """
    Get peers older than now - PEER_LIFETIME
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    if now is None:
        now = time.time()

    expire = now - atlas_peer_max_age()
    sql = "SELECT * FROM peers WHERE discovery_time < ?";
    args = (expire,)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    rows = []
    for row in res:
        tmp = {}
        tmp.update(row)
        rows.append(tmp)

    if close:
        con.close()

    return rows


def atlasdb_renew_peer( peer_hostport, now, con=None, path=None ):
    """
    Renew a peer's discovery time
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    if now is None:
        now = time.time()

    expire = now - atlas_peer_max_age()
    sql = "UPDATE peers SET discovery_time = ? WHERE peer_hostport = ?;"
    args = (now, peer_hostport)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    if close:
        con.close()

    return True


def atlasdb_load_peer_table( peer_hostport, con=None, path=None ):
    """
    Create a peer table from the peer DB
    """
    peer_table = {}
    
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "SELECT * FROM peers;"
    args = ()

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    # build it up 
    count = 0
    for row in res:
       if count > 0 and count % 100 == 0:
           log.debug("Loaded %s peers..." % count)

       atlas_init_peer_info( peer_table, row['peer_hostport'] )
       count += 1

    if close:
        con.close()

    return peer_table


def atlasdb_init( path, db, peer_seeds, peer_blacklist, validate=False, zonefile_dir=None ):
    """
    Set up the atlas node:
    * create the db if it doesn't exist
    * go through all the names and verify that we have the *current* zonefiles
    * if we don't, queue them for fetching.
    * set up the peer db

    @db should be an instance of BlockstackDB
    @initial_peers should be a list of URLs

    Return the newly-initialized peer table
    """
    
    global ATLASDB_SQL

    peer_table = {}

    if os.path.exists( path ):
        log.debug("Atlas DB exists at %s" % path)
        
        atlasdb_last_block = atlasdb_get_lastblock( path=path )
        if atlasdb_last_block is None:
            atlasdb_last_block = 0

        log.debug("Synchronize zonefiles from %s to %s" % (atlasdb_last_block, db.lastblock) )

        con = sqlite3.connect( path, isolation_level=None )
        atlasdb_zonefile_block_reset( con, db, atlasdb_last_block, path=path )
        atlasdb_queue_zonefiles( con, db, atlasdb_last_block, validate=validate, zonefile_dir=zonefile_dir )

        log.debug("Refreshing seed peers")
        for peer in peer_seeds:
            # forcibly add seed peers
            atlasdb_add_peer( peer, con=con, peer_table=peer_table, ping_on_evict=False )

        # load up peer table from the db
        log.debug("Loading peer table")
        peer_table = atlasdb_load_peer_table( con )

        # cache zonefile inventory and count
        atlasdb_cache_zonefile_info( con=con )
        con.close()

    else:

        log.debug("Initializing Atlas DB at %s" % path)

        lines = [l + ";" for l in ATLASDB_SQL.split(";")]
        con = sqlite3.connect( path, isolation_level=None )

        for line in lines:
            con.execute(line)

        con.row_factory = atlasdb_row_factory

        # populate from db
        log.debug("Queuing all zonefiles")
        atlasdb_queue_zonefiles( con, db, FIRST_BLOCK_MAINNET, validate=validate, zonefile_dir=zonefile_dir )

        log.debug("Adding seed peers")
        for peer in peer_seeds:
            atlasdb_add_peer( peer, con=con, peer_table=peer_table )

        atlasdb_cache_zonefile_info( con=con )
        con.close()

    # whitelist and blacklist
    for peer_url in peer_seeds:
        host, port = url_to_host_port( peer_url )
        peer_hostport = "%s:%s" % (host, port)

        if peer_hostport not in peer_table.keys():
            atlasdb_add_peer( peer_hostport, path=path, peer_table=peer_table )

        peer_table[peer_hostport]['whitelisted'] = True

    for peer_url in peer_blacklist:
        host, port = url_to_host_port( peer_url )
        peer_hostport = "%s:%s" % (host, port)

        if peer_hostport not in peer_table.keys():
            atlasdb_add_peer( peer_hostport, path=path, peer_table=peer_table )
        
        peer_table[peer_hostport]['blacklisted'] = True

    return peer_table


def atlas_peer_table_init( initial_peer_table ):
    """
    Set the initial peer table
    (usually the value returned by atlasdb_init)
    """
    global PEER_TABLE
    PEER_TABLE = initial_peer_table


def atlasdb_zonefile_inv_list( bit_offset, bit_length, con=None, path=None ):
    """
    Get an inventory listing.
    offset and length are in bits.

    Return the list of zonefile information.
    The list may be less than length elements.
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "SELECT * FROM zonefiles LIMIT ? OFFSET ?;"
    args = (bit_length, bit_offset)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    ret = []
    for row in res:
        tmp = {}
        tmp.update(row)
        ret.append(tmp)

    if close:
        con.close()

    return ret


def atlasdb_zonefile_inv_length( con=None, path=None ):
    """
    Find out how long our zonefile inventory vector is (in bits)
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "SELECT MAX(inv_index) FROM zonefiles;"
    args = ()

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    ret = []
    for row in res:
        try:
            if row[0] is None:
                ret.append( {'MAX(inv_index)': 0} )
                break

        except:
            pass

        tmp = {}
        tmp.update(row)
        ret.append(tmp)

    assert len(ret) == 1

    if close:
        con.close()

    if ret[0]['MAX(inv_index)'] is None:
        return 0

    else:
        return ret[0]['MAX(inv_index)'] + 1


def atlasdb_zonefile_find_missing( bit_offset, bit_count, con=None, path=None ):
    """
    Find out which zonefiles we're still missing.
    offset and count are *bit* indexes
    Return a list of zonefile rows, where present == 0.
    """
    if path is None:
        path = atlasdb_path()

    close = False
    if con is None:
        close = True
        con = atlasdb_open( path )
        assert con is not None

    sql = "SELECT * FROM zonefiles WHERE present = 0 LIMIT ? OFFSET ?;"
    args = (bit_count, bit_offset)

    cur = con.cursor()
    res = atlasdb_query_execute( cur, sql, args )
    con.commit()

    ret = []
    for row in res:
        tmp = {}
        tmp.update(row)
        ret.append(tmp)

    if close:
        con.close()

    return ret


def atlas_make_zonefile_inventory( bit_offset, bit_length, con=None, path=None ):
    """
    Get a summary description of the list of zonefiles we have
    for the given block range (a "zonefile inventory")

    Zonefile present/absent bits are ordered left-to-right,
    where the leftmost bit is the earliest zonefile in the blockchain.

    Offset and length are in bytes.

    This is slow.  Use the in-RAM zonefile inventory vector whenever possible
    (see atlas_get_zonefile_inventory).
    """
    
    listing = atlasdb_zonefile_inv_list( bit_offset, bit_length, con=con, path=path )

    # serialize to inv
    bool_vec = [l['present'] for l in listing]
    if len(bool_vec) % 8 != 0: 
        # pad 
        bool_vec += [False] * (8 - (len(bool_vec) % 8))

    inv = ""
    for i in xrange(0, len(bool_vec), 8):
        bit_vec = map( lambda b: 1 if b else 0, bool_vec[i:i+8] )
        next_byte = (bit_vec[0] << 7) | \
                    (bit_vec[1] << 6) | \
                    (bit_vec[2] << 5) | \
                    (bit_vec[3] << 4) | \
                    (bit_vec[4] << 3) | \
                    (bit_vec[5] << 2) | \
                    (bit_vec[6] << 1) | \
                    (bit_vec[7])
        inv += chr(next_byte)

    return inv


def atlas_get_zonefile_inventory( offset=None, length=None ):
    """
    Get the in-RAM zonefile inventory vector.
    """
    global ZONEFILE_INV

    try:
        assert ZONEFILE_INV is not None
    except AssertionError:
        log.error("FATAL: zonefile inventory not loaded")
        os.abort()

    if offset is None:
        offset = 0

    if length is None:
        length = len(ZONEFILE_INV) - offset

    if offset >= len(ZONEFILE_INV):
        return ""

    if offset + length > len(ZONEFILE_INV):
        length = len(ZONEFILE_INV) - offset
        
    ret = ZONEFILE_INV[offset:offset+length]
    return ret


def atlas_get_num_zonefiles():
    """
    Get the number of zonefiles we know about
    """
    global NUM_ZONEFILES
    return NUM_ZONEFILES


def atlas_init_peer_info( peer_table, peer_hostport, blacklisted=False, whitelisted=False ):
    """
    Initialize peer info table entry
    """
    peer_table[peer_hostport] = {
        "time": [],
        "zonefile_inv": "",
        "blacklisted": blacklisted,
        "whitelisted": whitelisted
    }


def atlas_log_socket_error( method_invocation, peer_hostport, se ):
    """
    Log a socket exception tastefully
    """
    if isinstance( se, socket.timeout ):
        log.debug("%s %s: timed out (socket.timeout)" % (method_invocation, peer_hostport))

    elif isinstance( se, socket.gaierror ):
        log.debug("%s %s: failed to query address or info (socket.gaierror)" % (method_invocation, peer_hostport ))

    elif isinstance( se, socket.herror ):
        log.debug("%s %s: failed to query host info (socket.herror)" % (method_invocation, peer_hostport ))

    elif isinstance( se, socket.error ):
        if se.errno == errno.ECONNREFUSED:
            log.debug("%s %s: is unreachable (socket.error ECONNREFUSED)" % (method_invocation, peer_hostport))
        elif se.errno == errno.ETIMEDOUT:
            log.debug("%s %s: timed out (socket.error ETIMEDOUT)" % (method_invocation, peer_hostport))
        else:
            log.debug("%s %s: socket error" % (method_invocation, peer_hostport))
            log.exception(se)

    else:
        log.debug("%s %s: general exception" % (method_invocation, peer_hostport))
        log.exception(se)


def atlas_peer_ping( peer_hostport, timeout=None, peer_table=None ):
    """
    Ping a host
    Return True if alive
    Return False if not
    """
    
    if timeout is None:
        timeout = atlas_ping_timeout()

    assert not atlas_peer_table_is_locked_by_me()

    host, port = url_to_host_port( peer_hostport )
    rpc = BlockstackRPCClient( host, port, timeout=timeout )

    log.debug("Ping %s" % peer_hostport)
    ret = False

    try:
        rpc.ping()
        ret = True
    except (socket.timeout, socket.gaierror, socket.herror, socket.error), se:
        atlas_log_socket_error( "ping(%s)" % peer_hostport, peer_hostport, se )
        pass

    except Exception, e:
        log.exception(e)
        pass

    # update health
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    if peer_table.has_key(peer_hostport):
        atlas_peer_update_health( peer_hostport, ret, peer_table=peer_table )

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return ret


def atlas_peer_getinfo( peer_hostport, timeout=None, peer_table=None ):
    """
    Get host info
    Return True if alive
    Return False if not
    """

    if timeout is None:
        timeout = atlas_ping_timeout()

    host, port = url_to_host_port( peer_hostport )
    rpc = BlockstackRPCClient( host, port, timeout=timeout )

    assert not atlas_peer_table_is_locked_by_me()

    log.debug("getinfo %s" % peer_hostport)
    res = None

    try:
        res = rpc.getinfo()
        assert type(res) in [dict], 'Did not receive a dict'

        if 'error' not in res:
            for req_field in ['consensus', 'server_version', 'last_block_processed']:
                assert req_field in res.keys(), "Missing field '%s'" % req_field
                assert type(res[req_field]) in [str, unicode, int], "Not a string or int: '%s'" % req_field
                res[req_field] = str(res[req_field])

        else:
            log.error("Failed to getinfo on %s: %s" % (peer_hostport, res['error']))
            res = None
                
    except (socket.timeout, socket.gaierror, socket.herror, socket.error), se:
        atlas_log_socket_error( "getinfo(%s)" % peer_hostport, peer_hostport, se )

    except AssertionError, ae:
        log.exception(ae)
        log.error("Invalid server reply for getinfo from %s" % peer_hostport)

    except Exception, e:
        log.exception(e)
        log.error("Failed to get response from %s" % peer_hostport)

    # update health
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    if peer_table.has_key(peer_hostport):
        atlas_peer_update_health( peer_hostport, (res is not None), peer_table=peer_table )

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return res



def atlas_inventory_count_missing( inv1, inv2 ):
    """
    Find out how many bits are set in inv2 
    that are not set in inv1.
    """
    count = 0
    common = min(len(inv1), len(inv2))
    for i in xrange(0, common):
        for j in xrange(0, 8):
            if ((1 << (7 - j)) & ord(inv2[i])) != 0 and ((1 << (7 - j)) & ord(inv1[i])) == 0:
                count += 1

    if len(inv1) < len(inv2):
        for i in xrange(len(inv1), len(inv2)):
            for j in xrange(0, 8):
                if ((1 << (7 - j)) & ord(inv2[i])) != 0:
                    count += 1

    return count


def atlas_get_live_neighbors( remote_peer_hostport, peer_table=None, min_health=MIN_PEER_HEALTH, min_request_count=1 ):
    """
    Get a random set of live neighbors
    (i.e. neighbors we've contacted before)
    """

    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    alive_peers = []
    for peer_hostport in peer_table.keys():
        if peer_hostport == remote_peer_hostport:
            continue

        num_reqs = atlas_peer_get_request_count( peer_hostport, peer_table=peer_table )
        if num_reqs < min_request_count:
            continue

        health = atlas_peer_get_health( peer_hostport, peer_table=peer_table )
        if health < min_health:
            continue

        alive_peers.append( peer_hostport )

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    random.shuffle(alive_peers)
    return alive_peers


def atlas_get_all_neighbors( peer_table=None ):
    """
    Get *all* neighbor information.
    USED ONLY FOR TESTING
    """
    if os.environ.get("BLOCKSTACK_ATLAS_NETWORK_SIMULATION") != "1":
        raise Exception("This method is only available when testing with the Atlas network simulator")

    ret = {}
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    ret = copy.deepcopy(peer_table)

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    # make zonefile inventories printable
    for peer_hostport in ret.keys():
        if ret[peer_hostport].has_key('zonefile_inv'):
            ret[peer_hostport]['zonefile_inv'] = atlas_inventory_to_string( ret[peer_hostport]['zonefile_inv'] )

    return ret


def atlas_revalidate_peers( con=None, path=None, now=None, peer_table=None ):
    """
    Revalidate peers that are older than the maximum peer age.
    Ping them, and if they don't respond, remove them.
    """
    global MIN_PEER_HEALTH

    if now is None:
        now = time_now()

    old_peer_infos = atlasdb_get_old_peers( now, con=con, path=path )
    for old_peer_info in old_peer_infos:
        res = atlas_peer_ping( old_peer_info['peer_hostport'] )
        if not res:
            log.debug("Failed to revalidate %s" % (old_peer_info['peer_hostport']))
            if atlas_peer_is_whitelisted( old_peer_info['peer_hostport'], peer_table=peer_table ):
                continue

            if atlas_peer_is_blacklisted( old_peer_info['peer_hostport'], peer_table=peer_table ):
                continue

            if atlas_peer_get_health( old_peer_info['peer_hostport'], peer_table=peer_table ) < MIN_PEER_HEALTH:
                atlasdb_remove_peer( old_peer_info['peer_hostport'], con=con, path=path, peer_table=peer_table )
        
        else:
            # renew 
            atlasdb_renew_peer( old_peer_info['peer_hostport'], now, con=con, path=path )

    return True


def atlas_peer_get_health( peer_hostport, peer_table=None ):
    """
    Get the health score for a peer.
    Health is: (number of responses received / number of requests sent) 
    """
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    # availability score: number of responses / number of requests
    num_responses = 0
    num_requests = 0
    if peer_table.has_key(peer_hostport):
        for (t, r) in peer_table[peer_hostport]['time']:
            num_requests += 1
            if r:
                num_responses += 1

    availability_score = 0.0
    if num_requests > 0:
        availability_score = float(num_responses) / float(num_requests)

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return availability_score


def atlas_peer_get_request_count( peer_hostport, peer_table=None ):
    """
    How many times have we contacted this peer?
    """
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    if peer_hostport not in peer_table.keys():
        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        return 0

    count = 0
    for (t, r) in peer_table[peer_hostport]['time']:
        if r:
            count += 1

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return count


def atlas_peer_get_zonefile_inventory( peer_hostport, peer_table=None ):
    """
    What's the zonefile inventory vector for this peer?
    Return None if not defined
    """
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    if peer_hostport not in peer_table.keys():
        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        return None

    inv = peer_table[peer_hostport]['zonefile_inv']

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return inv


def atlas_peer_set_zonefile_inventory( peer_hostport, peer_inv, peer_table=None ):
    """
    Set this peer's zonefile inventory
    """
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    if peer_hostport not in peer_table.keys():
        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        return None 

    peer_table[peer_hostport]['zonefile_inv'] = peer_inv

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return peer_inv


def atlas_peer_is_blacklisted( peer_hostport, peer_table=None ):
    """
    Is a peer blacklisted?
    """
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    if peer_hostport not in peer_table.keys():
        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        return None 

    ret = peer_table[peer_hostport].get("blacklisted", False)

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return ret


def atlas_peer_is_whitelisted( peer_hostport, peer_table=None ):
    """
    Is a peer whitelisted
    """
    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    if peer_hostport not in peer_table.keys():
        if locked:
            atlas_peer_table_unlock()
            peer_table = None
        
        return None 

    ret = peer_table[peer_hostport].get("whitelisted", False)

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return ret


def atlas_peer_update_health( peer_hostport, received_response, peer_table=None ):
    """
    Mark the given peer as alive at this time.
    Update times at which we contacted it,
    and update its health score.

    Use the global health table by default, 
    or use the given health info if set.
    """

    locked = False
    if peer_table is None:
        locked = True 
        peer_table = atlas_peer_table_lock()

    if peer_hostport not in peer_table.keys():
        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        return False

    # record that we contacted this peer, and whether or not we useful info from it
    now = time_now()

    # update timestamps; remove old data
    new_times = []
    for (t, r) in peer_table[peer_hostport]['time']:
        if t + atlas_peer_lifetime_interval() < now:
            continue
        
        new_times.append((t, r))

    new_times.append((now, received_response))
    peer_table[peer_hostport]['time'] = new_times

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return True


def atlas_peer_get_zonefile_inventory_range( my_hostport, peer_hostport, bit_offset, bit_count, timeout=None, peer_table=None ):
    """
    Get the zonefile inventory bit vector for a given peer.
    The returned range will be [bit_offset, bit_offset+count]

    Update peer health information as well.
    
    bit_offset and bit_count are in bits.

    Return the bit vector on success (padded to the nearest byte with 0's).
    Return None if we couldn't contact the peer.
    """

    if timeout is None:
        timeout = atlas_inv_timeout()

    host, port = url_to_host_port( peer_hostport )
    rpc = BlockstackRPCClient( host, port, timeout=timeout, src=my_hostport )

    zf_inv = {}
    zf_inv_list = None
    
    assert not atlas_peer_table_is_locked_by_me()

    zf_inv = None

    log.debug("Get zonefile inventory range %s-%s from %s" % (bit_offset, bit_count, peer_hostport))
    try:
        zf_inv = rpc.get_zonefile_inventory( bit_offset, bit_count )
        
        # sanity check
        assert type(zf_inv) == dict, "Inventory is not a dict"
        if 'error' not in zf_inv.keys():
            assert 'status' in zf_inv, "Invalid inv reply"
            assert zf_inv['status'], "Invalid inv reply"
            assert 'inv' in zf_inv, "Invalid inv reply"
            assert type(zf_inv['inv']) in [str, unicode], "Invalid inv bit field"

            try:
                zf_inv['inv'] = base64.b64decode( str(zf_inv['inv']) )
            except:
                raise AssertionError("Inv is not base64")

            # make sure it corresponds to this range
            assert len(zf_inv['inv']) <= (bit_count / 8) + (bit_count % 8), "Zonefile inventory in is too long" 

        else:
            assert type(zf_inv['error']) in [str, unicode], "Invalid error message"

    except (socket.timeout, socket.gaierror, socket.herror, socket.error), se:
        atlas_log_socket_error( "get_zonefile_inventory(%s, %s, %s)" % (peer_hostport, bit_offset, bit_count), peer_hostport, se )
        log.error("Failed to ask %s for zonefile inventory over %s-%s (socket-related error)" % (peer_hostport, bit_offset, bit_count))
        
    except Exception, e:
        if os.environ.get("BLOCKSTACK_DEBUG") == "1":
            log.exception(e)

        log.error("Failed to ask %s for zonefile inventory over %s-%s" % (peer_hostport, bit_offset, bit_count))

    atlas_peer_update_health( peer_hostport, (zf_inv is not None and zf_inv.has_key('status') and zf_inv['status']), peer_table=peer_table )

    if zf_inv is None:
        log.error("No inventory given for %s-%s from %s" % (bit_offset, bit_count, peer_hostport))
        return None 

    if 'error' in zf_inv:
        log.error("Failed to get inventory for %s-%s from %s: %s" % (bit_offset, bit_count, peer_hostport, zf_inv['error']))
        return None

    else:
        log.debug("Zonefile inventory for %s (%s-%s) is '%s'" % (peer_hostport, bit_offset, bit_count, atlas_inventory_to_string(zf_inv['inv'])))
        return zf_inv['inv']


def atlas_peer_download_zonefile_inventory( my_hostport, peer_hostport, maxlen, bit_offset=0, timeout=None, peer_table={} ):
    """
    Get the zonefile inventory from the remote peer
    Start from the given bit_offset

    NOTE: this doesn't update the peer table health by default;
    you'll have to explicitly pass in a peer table (i.e. setting
    to {} ensures that nothing happens).
    """

    if timeout is None:
        timeout = atlas_inv_timeout()

    interval = 524288       # number of bits in 64KB
    peer_inv = ""

    log.debug("Download zonefile inventory %s-%s from %s" % (bit_offset, maxlen, peer_hostport))

    if bit_offset > maxlen:
        # synced already
        return peer_inv

    for offset in xrange( bit_offset, maxlen, interval):
        next_inv = atlas_peer_get_zonefile_inventory_range( my_hostport, peer_hostport, offset, interval, timeout=timeout, peer_table=peer_table )
        if next_inv is None:
            # partial failure
            log.debug("Failed to sync inventory for %s from %s to %s" % (peer_hostport, offset, offset+interval))
            break

        peer_inv += next_inv
        if len(next_inv) < interval:
            # end-of-interval
            break

    return peer_inv



def atlas_peer_sync_zonefile_inventory( my_hostport, peer_hostport, maxlen, timeout=None, peer_table=None ):
    """
    Synchronize our knowledge of a peer's zonefiles up to a given byte length
    NOT THREAD SAFE; CALL FROM ONLY ONE THREAD.

    maxlen is the maximum length in bits of the expected zonefile.

    Return the new inv vector if we synced it (updating the peer table in the process)
    Return None if not
    """
    if timeout is None:
        timeout = atlas_inv_timeout()

    peer_inv = ""

    locked = False
    if peer_table is None:
        locked = True    
        peer_table = atlas_peer_table_lock()

    if peer_hostport not in peer_table.keys():
        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        return None 

    peer_inv = atlas_peer_get_zonefile_inventory( peer_hostport, peer_table=peer_table )

    bit_offset = (len(peer_inv) - 1) * 8      # i.e. re-obtain the last byte
    if bit_offset < 0:
        bit_offset = 0

    else:
        peer_inv = peer_inv[:-1]

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    peer_inv = atlas_peer_download_zonefile_inventory( my_hostport, peer_hostport, maxlen, bit_offset=bit_offset, timeout=timeout, peer_table=peer_table )
   
    if locked:
        peer_table = atlas_peer_table_lock()

    if peer_hostport not in peer_table.keys():
        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        log.debug("%s no longer a peer" % peer_hostport)
        return None 

    log.debug("Set zonefile inventory %s: %s" % (peer_hostport, atlas_inventory_to_string(peer_inv)))
    atlas_peer_set_zonefile_inventory( peer_hostport, peer_inv, peer_table=peer_table ) # NOTE: may have trailing 0's for padding

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return peer_inv


def atlas_peer_refresh_zonefile_inventory( my_hostport, peer_hostport, byte_offset, timeout=None, peer_table=None, con=None, path=None, local_inv=None ):
    """
    Refresh a peer's zonefile recent inventory vector entries,
    by removing every bit after byte_offset and re-synchronizing them.

    The intuition here is that recent zonefiles are much rarer than older
    zonefiles (which will have been near-100% replicated), meaning the tail
    of the peer's zonefile inventory is a lot less stable than the head (since
    peers will be actively distributing recent zonefiles).

    NOT THREAD SAFE; CALL FROM ONLY ONE THREAD.

    Return True if we synced all the way up to the expected inventory length, and update the refresh time in the peer table.
    Return False if not.
    """

    if timeout is None:
        timeout = atlas_inv_timeout()

    if local_inv is None:
        # get local zonefile inv 
        inv_len = atlasdb_zonefile_inv_length( con=con, path=path )
        local_inv = atlas_make_zonefile_inventory( 0, inv_len, con=con, path=path )

    maxlen = len(local_inv)

    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    if peer_hostport not in peer_table.keys():
        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        return False

    # reset the peer's zonefile inventory, back to offset
    cur_inv = atlas_peer_get_zonefile_inventory( peer_hostport, peer_table=peer_table )
    atlas_peer_set_zonefile_inventory( peer_hostport, cur_inv[:byte_offset], peer_table=peer_table )

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    inv = atlas_peer_sync_zonefile_inventory( my_hostport, peer_hostport, maxlen, timeout=timeout, peer_table=peer_table )

    if inv is not None:
        # success!  Update refresh time
        if locked:
            peer_table = atlas_peer_table_lock()

        if peer_hostport not in peer_table.keys():
            if locked:
                atlas_peer_table_unlock()
                peer_table = None
            
            return False

        peer_table[peer_hostport]['zonefile_inventory_last_refresh'] = time_now()

        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        log.debug("%s: inventory of %s is now '%s'" % (my_hostport, peer_hostport, atlas_inventory_to_string(inv))) 

    if inv is None:
        return False

    else:
        return True


def atlas_peer_has_fresh_zonefile_inventory( peer_hostport, local_inv=None, peer_table=None, con=None, path=None ):
    """
    Does the given atlas node have a fresh zonefile inventory?
    """

    if local_inv is None:
        # get local zonefile inv 
        inv_len = atlasdb_zonefile_inv_length( con=con, path=path )
        local_inv = atlas_make_zonefile_inventory( 0, inv_len, con=con, path=path )

    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    if peer_hostport not in peer_table.keys():
        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        return False

    expected_length = len(local_inv)

    fresh = False
    now = time_now()
    peer_inv = atlas_peer_get_zonefile_inventory( peer_hostport, peer_table=peer_table )
    if len(peer_inv) >= expected_length and \
        peer_table[peer_hostport].has_key('zonefile_inventory_last_refresh') and \
        peer_table[peer_hostport]['zonefile_inventory_last_refresh'] + atlas_peer_ping_interval() > now:

        fresh = True

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return fresh


def atlas_peer_set_zonefile_status( peer_hostport, zonefile_hash, present, zonefile_bits=None, peer_table=None, con=None, path=None ):
    """
    Mark a zonefile as being present or absent on a peer.
    Use this method to update our knowledge of what other peers have,
    based on when we try to ask them for zonefiles (i.e. a peer can
    lie about what zonefiles it has, and if it advertizes the availability
    of a zonefile but doesn't deliver, then we need to remember not
    to ask it again).
    """
    if zonefile_bits is None:
        zonefile_bits = atlasdb_get_zonefile_bits( zonefile_hash, con=con, path=path )

    locked = False
    if peer_table is None:
        locked = True    
        peer_table = atlas_peer_table_lock()

    if peer_table.has_key(peer_hostport):
        peer_inv = atlas_peer_get_zonefile_inventory( peer_hostport, peer_table=peer_table )
        peer_inv = atlas_inventory_flip_zonefile_bits( peer_inv, zonefile_bits, present )
        atlas_peer_set_zonefile_inventory( peer_hostport, peer_inv, peer_table=peer_table )
                
    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return


def atlas_find_missing_zonefile_availability( peer_table=None, con=None, path=None, missing_zonefile_info=None ):
    """
    Find the set of missing zonefiles, as well as their popularity amongst 
    our neighbors.

    Only consider zonefiles that are known by at least
    one peer; otherwise they're missing from
    our clique (and we'll re-sync our neighborss' inventories
    every so often to make sure we detect when zonefiles
    become available).

    Return a dict, structured as:
    {
        'zonefile hash': {
            'indexes': [...],
            'popularity': ...,
            'peers': [...],
            'tried_storage': True|False
        }
    }
    """

    # which zonefiles do we have?
    bit_offset = 0
    bit_count = 10000
    missing = []
    ret = {}

    if missing_zonefile_info is None:
        while True:
            zfinfo = atlasdb_zonefile_find_missing( bit_offset, bit_count, con=con, path=path )
            if len(zfinfo) == 0:
                break

            missing += zfinfo
            bit_offset += len(zfinfo)

    else:
        missing = missing_zonefile_info

    if len(missing) == 0:
        # none!
        return ret

    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    # do any other peers have this zonefile?
    for zfinfo in missing:
        popularity = 0
        byte_index = (zfinfo['inv_index'] - 1) / 8
        bit_index = 7 - ((zfinfo['inv_index'] - 1) % 8)
        peers = []

        if not ret.has_key(zfinfo['zonefile_hash']):
            ret[zfinfo['zonefile_hash']] = {
                'indexes': [],
                'popularity': 0,
                'peers': []
            }

        for peer_hostport in peer_table.keys():
            peer_inv = atlas_peer_get_zonefile_inventory( peer_hostport, peer_table=peer_table )
            if len(peer_inv) <= byte_index:
                # too new for this peer
                continue

            if (ord(peer_inv[byte_index]) & (1 << bit_index)) == 0:
                # this peer doesn't have it
                continue

            if peer_hostport not in ret[zfinfo['zonefile_hash']]['peers']:
                popularity += 1
                peers.append( peer_hostport )

        ret[zfinfo['zonefile_hash']]['indexes'].append( zfinfo['inv_index']-1 )
        ret[zfinfo['zonefile_hash']]['popularity'] += popularity
        ret[zfinfo['zonefile_hash']]['peers'] += peers
        ret[zfinfo['zonefile_hash']]['tried_storage'] = (zfinfo['tried_storage'] != 0)

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return ret


def atlas_peer_has_zonefile( peer_hostport, zonefile_hash, zonefile_bits=None, con=None, path=None, peer_table=None ):
    """
    Does the given peer have the given zonefile defined?
    Check its inventory vector

    Return True if present
    Return False if not present
    Return None if we don't know about the zonefile ourselves, or if we don't know about the peer
    """

    bits = None
    if zonefile_bits is None:
        bits = atlasdb_get_zonefile_bits( zonefile_hash, con=con, path=path )
        if len(bits) == 0:
            return None

    else:
        bits = zonefile_bits

    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    if peer_hostport not in peer_table.keys():
        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        return False

    zonefile_inv = atlas_peer_get_zonefile_inventory( peer_hostport, peer_table=peer_table )
    
    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    res = atlas_inventory_test_zonefile_bits( zonefile_inv, bits )
    return res


def atlas_peer_get_neighbors( my_hostport, peer_hostport, timeout=None, peer_table=None, con=None, path=None ):
    """
    Ask the peer server at the given URL for its neighbors.

    Update the health info in peer_table
    (if not given, the global peer table will be used instead)

    Return the list on success
    Return None on failure to contact
    Raise on invalid URL
    """
   
    if timeout is None:
        timeout = atlas_neighbors_timeout()

    host, port = url_to_host_port( peer_hostport )
    if host is None or port is None:
        log.debug("Invalid host/port %s" % peer_hostport)
        raise ValueError("Invalid host/port %s" % peer_hostport)

    rpc = BlockstackRPCClient( host, port, timeout=timeout, src=my_hostport )
    peer_list = None

    # sane limits
    max_neighbors = atlas_max_neighbors()

    assert not atlas_peer_table_is_locked_by_me()

    try:
        peer_list = rpc.get_atlas_peers()

        assert type(peer_list) in [dict], "Not a peer list response"

        if 'error' not in peer_list:
            assert 'status' in peer_list, "No status in response"
            assert 'peers' in peer_list, "No peers in response"
            assert type(peer_list['peers']) in [list], "Not a peer list"
            for peer in peer_list['peers']:
                assert type(peer) in [str, unicode], "Invalid peer list"

            assert len(peer_list['peers']) <= max_neighbors, "Invalid response with too many peers (%s, expected <= %s)" % (len(peer_list['peers']), max_neighbors)

        else:
            assert type(peer_list['error']) in [str, unicode], "Invalid error message"

    except (socket.timeout, socket.gaierror, socket.herror, socket.error), se:
        atlas_log_socket_error( "get_atlas_peers(%s)" % peer_hostport, peer_hostport, se)
        log.error("Socket error in response from '%s'" % peer_hostport)

    except AssertionError, ae:
        if os.environ.get("BLOCKSTACK_DEBUG") == "1":
            log.exception(ae)
        log.error("Invalid peer list response from '%s'" % peer_hostport)

    except Exception, e:
        if os.environ.get("BLOCKSTACK_DEBUG") == "1":
            log.exception(e)
        log.error("Failed to talk to '%s'" % peer_hostport)
   
    if peer_list is None:
        log.error("Failed to query remote peer %s" % peer_hostport)
        atlas_peer_update_health( peer_hostport, False, peer_table=peer_table )
        return None 

    if 'error' in peer_list:
        log.debug("Remote peer error: %s" % peer_list['error'])
        log.error("Remote peer error on %s" % peer_hostport)
        atlas_peer_update_health( peer_hostport, False, peer_table=peer_table )
        return None

    ret = peer_list['peers']
    atlas_peer_update_health( peer_hostport, True, peer_table=peer_table )
    return ret


def atlas_get_zonefiles( my_hostport, peer_hostport, zonefile_hashes, timeout=None, peer_table=None ):
    """
    Given a list of zonefile hashes.
    go and get them from the given host.

    Update node health

    Return the newly-fetched zonefiles on success (as a dict mapping hashes to zonefile data)
    Return None on error.
    """

    if timeout is None:
        timeout = atlas_zonefiles_timeout()

    host, port = url_to_host_port( peer_hostport )
    rpc = BlockstackRPCClient( host, port, timeout=timeout, src=my_hostport )
    zf_payload = None

    assert not atlas_peer_table_is_locked_by_me()

    try:
        zf_payload = rpc.get_zonefiles( zonefile_hashes )
        assert type(zf_payload) == dict, "Invalid zonefile listing"
        if 'error' not in zf_payload.keys():
            assert 'status' in zf_payload.keys(), "Invalid zonefile reply"
            assert zf_payload['status'], "Invalid zonefile reply"

            assert 'zonefiles' in zf_payload.keys(), "No zonefiles"
            zonefiles = zf_payload['zonefiles']

            assert type(zonefiles) == dict, "Invalid zonefiles: type %s" % type(zonefiles)
            for zf_hash, zf_data in zonefiles.items():
                assert type(zf_hash) in [str, unicode], "Invalid zonefile hash"
                assert len(zf_hash) == 2 * LENGTHS['update_hash'], "Invalid zonefile hash length"

                assert type(zf_data) in [str, unicode], "Invalid zonefile data"
                assert verify_zonefile( zf_data, zf_hash ), "Zonefile does not match or is not current" 

        else:
            assert type(zf_payload['error']) in [str, unicode], "Invalid error message"

    except (socket.timeout, socket.gaierror, socket.herror, socket.error), se:
        atlas_log_socket_error( "get_zonefiles(%s)" % peer_hostport, peer_hostport, se)

    except Exception, e:
        if os.environ.get("BLOCKSTACK_DEBUG") == "1":
            log.exception(e)

        log.error("Invalid zonefile data from %s" % peer_hostport)

    if zf_payload is None:
        log.error("Failed to fetch zonefile data from %s" % peer_hostport)
        atlas_peer_update_health( peer_hostport, False, peer_table=peer_table )
        return None 

    if 'error' in zf_payload.keys():
        log.error("Failed to fetch zonefile data from %s: %s" % (peer_hostport, zf_payload['error']))
        atlas_peer_update_health( peer_hostport, False, peer_table=peer_table )
        return None 

    atlas_peer_update_health( peer_hostport, True, peer_table=peer_table )
    return zf_payload['zonefiles']


def atlas_get_zonefile_from_storage( zonefile_hash, storage_drivers ):
    """
    Go get a zonefile from storage drivers
    """
    try:
        res = get_zonefile_from_storage( zonefile_hash, drivers=storage_drivers)
        return {'status': True, 'zonefile': res}
    except Exception, e:
        if os.environ.get("BLOCKSTACK_DEBUG", None) == "1":
            log.exception(e)

        return {'error': 'Failed to get zonefile from storage'}
    

def atlas_rank_peers_by_health( peer_list=None, peer_table=None, with_zero_requests=False, with_rank=False ):
    """
    Get a ranking of peers to contact for a zonefile.
    Peers are ranked by health (i.e. response ratio).

    Optionally include peers we haven't talked to yet (@with_zero_requests)
    Optionally return [(health, peer)] list instead of just [peer] list (@with_rank)
    """

    locked = False
    if peer_table is None:
        locked = True    
        peer_table = atlas_peer_table_lock()

    if peer_list is None:
        peer_list = peer_table.keys()[:]

    peer_health_ranking = []    # (health score, peer hostport)
    for peer_hostport in peer_list:
        reqcount = atlas_peer_get_request_count( peer_hostport, peer_table=peer_table )
        if reqcount == 0 and not with_zero_requests:
            continue

        health_score = atlas_peer_get_health( peer_hostport, peer_table=peer_table)
        peer_health_ranking.append( (health_score, peer_hostport) )
    
    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    # sort on health
    peer_health_ranking.sort()
    peer_health_ranking.reverse()

    if not with_rank:
        return [peer_hp for _, peer_hp in peer_health_ranking]
    else:
        # include the score.
        return peer_health_ranking


def atlas_rank_peers_by_data_availability( peer_list=None, peer_table=None, local_inv=None, con=None, path=None ):
    """
    Get a ranking of peers to contact for a zonefile.
    Peers are ranked by the number of zonefiles they have
    which we don't have.

    This is used to select neighbors.
    """

    locked = False
    if peer_table is None:
        locked = True    
        peer_table = atlas_peer_table_lock()

    if peer_list is None:
        peer_list = peer_table.keys()[:]

    if local_inv is None:
        # what's my inventory?
        inv_len = atlasdb_zonefile_inv_length( con=con, path=path )
        local_inv = atlas_make_zonefile_inventory( 0, inv_len, con=con, path=path )

    peer_availability_ranking = []    # (health score, peer hostport)
    for peer_hostport in peer_list:

        peer_inv = atlas_peer_get_zonefile_inventory( peer_hostport, peer_table=peer_table )

        # ignore peers that we don't have an inventory for
        if len(peer_inv) == 0:
            continue

        availability_score = atlas_inventory_count_missing( local_inv, peer_inv )
        peer_availability_ranking.append( (availability_score, peer_hostport) )
    
    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    # sort on availability
    peer_availability_ranking.sort()
    peer_availability_ranking.reverse()

    return [peer_hp for _, peer_hp in peer_availability_ranking]


def atlas_peer_enqueue( peer_hostport, peer_table=None, peer_queue=None, max_neighbors=None ):
    """
    Begin talking to a new peer, if we aren't already.
    Don't accept this peer if there are already too many peers in the incoming queue
    (where "too many" means "more than the maximum neighbor set size")

    Return True if added
    Return False if not added
    """

    peer_lock = False
    table_lock = False

    if peer_table is None:
        table_lock = True
        peer_table = atlas_peer_table_lock()

    present = (peer_hostport in peer_table.keys())

    if table_lock:
        atlas_peer_table_unlock()
        peer_table = None

    if present:
        # nothing to do 
        return False

    if peer_queue is None:
        peer_lock = True
        peer_queue = atlas_peer_queue_lock()

    res = False
    if not present:
        if max_neighbors is None:
            max_neighbors = atlas_max_neighbors()

        if len(peer_queue) < atlas_max_new_peers(max_neighbors):
            peer_queue.append( peer_hostport )
            res = True

    if peer_lock:
        atlas_peer_queue_unlock()
        peer_queue = None

    return res


def atlas_peer_dequeue_all( peer_queue=None ):
    """
    Get all queued peers
    """
    peer_lock = False

    if peer_queue is None:
        peer_lock = True
        peer_queue = atlas_peer_queue_lock()

    peers = []
    while len(peer_queue) > 0:
        peers.append( peer_queue.pop(0) )

    if peer_lock:
        atlas_peer_queue_unlock()
        peer_queue = None

    return peers


def atlas_zonefile_find_push_peers( zonefile_hash, peer_table=None, zonefile_bits=None, con=None, path=None ):
    """
    Find the set of peers that do *not* have this zonefile.
    """

    if zonefile_bits is None:
        zonefile_bits = atlasdb_get_zonefile_bits( zonefile_hash, path=path, con=con )
        if len(zonefile_bits) == 0:
            # we don't even know about it
            return []

    table_locked = False
    if peer_table is None:
        table_locked = True
        peer_table = atlas_peer_table_lock()

    push_peers = []
    for peer_hostport in peer_table.keys():
        zonefile_inv = atlas_peer_get_zonefile_inventory( peer_hostport, peer_table=peer_table )
        res = atlas_inventory_test_zonefile_bits( zonefile_inv, zonefile_bits )
        if res:
            push_peers.append( peer_hostport )

    if table_locked:
        atlas_peer_table_unlock()
        peer_table = None

    return push_peers


def atlas_zonefile_push_enqueue( zonefile_hash, zonefile_data, peer_table=None, zonefile_queue=None, con=None, path=None ):
    """
    Enqueue the given zonefile into our "push" queue.
    Only enqueue if we know of one peer that doesn't
    have it in its inventory vector.

    Return True if we enqueued it
    Return False if not
    """
    table_locked = False
    zonefile_queue_locked = False
    res = False

    bits = atlasdb_get_zonefile_bits( zonefile_hash, path=path, con=con )
    if len(bits) == 0:
        return

    if peer_table is None:
        table_locked = True
        peer_table = atlas_peer_table_lock()

    push_peers = atlas_zonefile_find_push_peers( zonefile_hash, peer_table=peer_table, zonefile_bits=bits )
    if len(push_peers) > 0:

        # someone needs this
        if zonefile_queue is None:
            zonefile_queue_locked = True
            zonefile_queue = atlas_zonefile_queue_lock()

        if len(zonefile_queue) < MAX_QUEUED_ZONEFILES: 
            zonefile_queue.append( {zonefile_hash: zonefile_data} )
            res = True
        
        if zonefile_queue_locked:
            atlas_zonefile_queue_unlock()
            
    if table_locked:
        atlas_peer_table_unlock()
        peer_table = None

    return res


def atlas_zonefile_push_dequeue( zonefile_queue=None ):
    """
    Dequeue a zonefile to replicate
    Return None if there are none queued
    """
    zonefile_queue_locked = False
    if zonefile_queue is None:
        zonefile_queue = atlas_zonefile_queue_lock()
        zonefile_queue_locked = True

    ret = None
    if len(zonefile_queue) > 0:
        ret = zonefile_queue.pop(0)

    if zonefile_queue_locked:
        atlas_zonefile_queue_unlock()

    return ret


def atlas_zonefile_push( my_hostport, peer_hostport, zonefile_dict, timeout=None, peer_table=None ):
    """
    Push the given zonefile to the given peer
    Return True on success
    Return False on failure
    """
    if timeout is None:
        timeout = atlas_push_zonefiles_timeout()
   
    zonefile_data = serialize_zonefile( zonefile_dict )
    zonefile_hash = blockstack_client.hash_zonefile( zonefile_dict )

    host, port = url_to_host_port( peer_hostport )
    rpc = BlockstackRPCClient( host, port, timeout=timeout, src=my_hostport )

    status = False

    assert not atlas_peer_table_is_locked_by_me()

    try:
        push_info = rpc.put_zonefiles( [zonefile_data] )

        assert type(push_info) in [dict], "Invalid push response"
        if 'status' in push_info and push_info['status']:
            assert 'saved' in push_info, "Missing saved vector"
            assert type(push_info['saved']) == list, "Invalid saved vector"
            assert len(push_info['saved']) == 1, "Invalid saved vector"
            assert push_info['saved'][0] in [0, 1], "Invalid saved vector value"

            if push_info['saved'] == 1:
                # woo!
                saved = True
            
    except (socket.timeout, socket.gaierror, socket.herror, socket.error), se:
        atlas_log_socket_error( "put_zonefiles(%s)" % peer_hostport, peer_hostport, se)
    
    except AssertionError, ae:
        log.exception(ae)
        log.error("Invalid server response from %s" % peer_hostport )

    except Exception, e:
        log.exception(e)
        log.error("Failed to push zonefile %s to %s" % (zonefile_hash, peer_hostport))

    locked = False
    if peer_table is None:
        locked = True
        peer_table = atlas_peer_table_lock()

    atlas_peer_update_health( peer_hostport, status, peer_table=peer_table )

    if locked:
        atlas_peer_table_unlock()
        peer_table = None

    return status
    

class AtlasPeerCrawler( threading.Thread ):
    """
    Thread that continuously crawls peers.

    Try to obtain knowledge of as many peers as we can.
    (but we will only report max NUM_NEIGHBORS peers to anyone who asks).
    The goals are (1) find an random, unbiased set of peers
    as our neighbors, and (2) add new peers to our neighbor set
    while imposing a high cost on an eclipse attack.

    Our peer discovery mechanism is carried out through a special
    type of random walk, starting from a (randomly-chosen) seed peer.
    At each peer, we ask for the neighbors, add the new neighbors
    to the peer db (if we can ping them first), and evict old neighbors
    if they collide with new neighbors (but only if the old neighbor
    is dead--i.e. it doesn't respond to a ping when we try to evict it).
    The collision is randomized--we hash the node's address with a 
    nonce, modulate by PEER_MAX_DB, and see if another row in the db
    has the same modulus.

    The random walk tries to account for bias towards selecting
    peers with high degree in the graph.  To do so, we execute
    a variation of Metropolis-Hastings to transition from the "current"
    peer to a "neighbor" peer, but with the following key differences:

    * If the neighbor peer's neighbors are all unresponsive, we pick a
    new peer from the peer DB.  There is no backtracing.
    * The transition probability isn't min(1, degree(neighbor)/degree(peer))
    like it is in MH.  Instead, we use Lee, Xu, and Eun's random walk algorithm
    MHRWDA (delayed acceptance Metropolis-Hastings random walk) (ACM SIGMETRICS 2012).

    The difference between this work and MHRWDA is that if we encounter
    a timeout or an unresponsive peer, we consider the walk to have "failed"
    and we restart from a randomly-chosen peer in our peer DB.
    """

    def __init__(self, my_hostname, my_portnum, path=None ):
        threading.Thread.__init__(self)
        self.running = False
        self.last_clean_time = 0
        self.my_hostport = "%s:%s" % (my_hostname, my_portnum)
        
        self.current_peer = None
        self.current_peer_neighbors = []

        self.prev_peer = None
        self.prev_peer_degree = 0

        self.new_peers = []
        self.max_neighbors = None
        self.atlasdb_path = path

        self.neighbors_timeout = None
        self.ping_timeout =  None


    def get_neighbors( self, peer_hostport, con=None, path=None, peer_table=None ):
        """
        Get neighbors of this peer
        """

        if self.neighbors_timeout is None:
            self.neighbors_timeout = atlas_neighbors_timeout()

        neighbors = None
        if peer_hostport == self.my_hostport:
            neighbors = atlas_get_live_neighbors( None, peer_table=peer_table ) 
        else:
            neighbors = atlas_peer_get_neighbors( self.my_hostport, peer_hostport, timeout=self.neighbors_timeout, peer_table=peer_table, path=path, con=con )

        if neighbors is not None:
            log.debug("%s: neighbors of %s are (%s): %s" % (self.my_hostport, peer_hostport, len(neighbors), ",".join(neighbors)))
        else:
            log.error("%s: failed to ask %s for neighbors" % (self.my_hostport, peer_hostport))
        return neighbors


    def add_new_peers( self, count, new_peers, current_peers, con=None, path=None, peer_table=None ):
        """
        Ping up to @count new peers from @new_peers 
        that aren't already known to us.  If they
        respond, then add them to the peer set.

        Return the list of peers added and the list of peers already known
        """

        if self.ping_timeout is None:
            self.ping_timeout = atlas_ping_timeout()

        # only handle a few peers for now
        cnt = 0
        i = 0
        added = []
        present = []
        while i < len(new_peers) and cnt < min(count, len(new_peers)):
            peer = new_peers[i]
            i += 1

            if peer == self.my_hostport:
                continue

            if peer in current_peers:
                log.debug("%s is already known" % peer)
                present.append(peer)
                continue 

            cnt += 1

            # test the peer before adding
            res = False
            if peer != self.my_hostport:
                res = atlas_peer_getinfo( peer, timeout=self.ping_timeout )
                if res is None:
                    # didn't respond
                    continue

                if not res.has_key('server_version'):
                    # too old
                    continue

                if semver_newer( res['server_version'], MIN_ATLAS_VERSION ):
                    # too old to be an atlas node
                    log.debug("%s is too old to be an atlas node (version %s)" % (peer, res['version']))
                    continue

                # TODO: check consensus hash as well

            if res:

                log.debug("Add newly-discovered peer %s" % peer)
                atlasdb_add_peer( peer, con=con, path=path, peer_table=peer_table )

            added.append(peer)

        return added, present


    def remove_unhealthy_peers( self, count, con=None, path=None, peer_table=None, min_request_count=10, min_health=MIN_PEER_HEALTH ):
        """
        Remove up to @count unhealthy peers
        Return the list of peers we removed
        """
        
        locked = False
        if peer_table is None:
            locked = True
            peer_table = atlas_peer_table_lock()

        removed = []
        rank_peer_list = atlas_rank_peers_by_health( peer_table=peer_table, with_rank=True )
        for rank, peer in rank_peer_list:
            reqcount = atlas_peer_get_request_count( peer, peer_table=peer_table )
            if reqcount >= min_request_count and rank < min_health:
                removed.append( peer )
                if len(removed) >= count:
                    break

        for peer in removed:
            log.debug("Remove unhealthy peer %s" % (peer))
            atlasdb_remove_peer( peer, con=con, path=path, peer_table=peer_table )

        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        return removed


    def random_walk_graph( self, prev_peer, prev_peer_degree, current_peer, current_peer_neighbors, con=None, path=None, peer_table=None ):
        """
        Take one step from current_peer to a neighbor in current_peer_neighbors,
        based on Metropolis-Hastings Random Walk with Delayed Acceptance (MHRWDA).

        The basic idea is to reduce the probability (versus MH alone) that we transition to the previous node.
        We do so using the Metropolis-Hastings Random Walk with Delated Acceptance (MHRWDA) algorithm
        described in Lee, Xu, and Eun in SIGMETRICS 2012.

        Return the next peer.
        """

        # the "next" current peer
        ret_current_peer = None
        ret_current_peer_neighbors = None

        error_ret = (None, None)
        
        current_peer_degree = len(current_peer_neighbors)
        if current_peer_degree == 0:
            # nowhere to go 
            log.debug("%s: current peer degree is 0" % (self.my_hostport))
            return error_ret

        next_peer = current_peer_neighbors[ random.randint(0, len(current_peer_neighbors)-1) ]
        next_peer_neighbors = self.get_neighbors( next_peer, con=con, path=path, peer_table=peer_table )
        if next_peer_neighbors is None or len(next_peer_neighbors) == 0:
            # walk failed, or nowhere to go
            # restart the walk
            log.debug("%s: failed to get neighbors of %s" % (self.my_hostport, next_peer))
            return error_ret

        next_peer_degree = len(next_peer_neighbors)

        p = random.random()
        if p <= min(1.0, float(current_peer_degree) / float(next_peer_degree)):
            if prev_peer == next_peer and current_peer_degree > 1:
                # find a different peer
                search = current_peer_neighbors[:]
                if next_peer in search:
                    search.remove(next_peer)

                alt_peer = search[ random.randint(0, len(search)-1) ]
                alt_peer_neighbors = self.get_neighbors( alt_peer, con=con, path=path, peer_table=peer_table )
                if alt_peer_neighbors is None or len(alt_peer_neighbors) == 0:
                    # walk failed, or nowhere to go
                    # restart the walk
                    log.debug("%s: failed to get neighbors of %s" % (self.my_hostport, alt_peer))
                    return error_ret

                alt_peer_degree = len(alt_peer_neighbors)

                q = random.random()
                if q <= min( 1.0, min( 1.0, (float(current_peer_degree) / float(alt_peer_degree))**2 ), max( 1.0, (float(prev_peer_degree) / float(current_peer_degree))**2 ) ):
                    # go to the alt peer instead
                    ret_current_peer = alt_peer
                    ret_current_peer_neighbors = alt_peer_neighbors

                else:
                    # go to next peer
                    ret_current_peer = next_peer
                    ret_current_peer_neighbors = next_peer_neighbors

            else:
                # go to next peer
                ret_current_peer = next_peer
                ret_current_peer_neighbors = next_peer_neighbors
        else:
            # stay here
            ret_current_peer = current_peer
            ret_current_peer_neighbors = self.get_neighbors( current_peer, con=con, path=path, peer_table=peer_table )
            if ret_current_peer_neighbors is None or len(ret_current_peer_neighbors) == 0:
                # nowhere to go
                log.debug("%s: failed to refresh %s" % (self.my_hostport, current_peer))
                return error_ret

        return (ret_current_peer, ret_current_peer_neighbors)
       

    def get_current_peers( self, peer_table=None ):
        """
        Get the current set of peers
        """
        # get current peers
        locked = False
        if peer_table is None:
            locked = True
            peer_table = atlas_peer_table_lock()

        current_peers = peer_table.keys()[:]

        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        return current_peers


    def update_new_peers( self, num_new_peers, current_peers, peer_queue=None, peer_table=None, con=None, path=None ):
        """
        Add at most $num_new_peers new peers from the pending peer queue to the peer DB.
        Ping them first (to see if they're alive), and drop hosts from the pending
        queue if it gets too long.
        Update our new peer queue, and update the peer table.

        Return the number of peers processed
        """

        # add newly-discovered peers, but only after we ping them
        # to make sure they're actually alive.
        peer_queue = atlas_peer_dequeue_all( peer_queue=peer_queue )

        new_peers = list(set(self.new_peers + peer_queue))
        random.shuffle( new_peers )

        # don't talk to myself
        if self.my_hostport in new_peers:
            new_peers.remove(self.my_hostport)

        # only handle a few peers for now
        log.debug("Add at most %s new peers out of %s options" % (num_new_peers, len(new_peers)))
        added, present = self.add_new_peers( num_new_peers, new_peers, current_peers, con=con, path=path, peer_table=peer_table )
        for peer in added + present:
            if peer in new_peers:
                new_peers.remove(peer)

            if peer in self.new_peers:
                self.new_peers.remove(peer)

        # DDoS prevention: don't let this get too big
        max_new_peers = atlas_max_new_peers( self.max_neighbors )
        if len(new_peers) > max_new_peers:
            new_peers = new_peers[:max_new_peers]

        self.new_peers = new_peers
        return len(added)


    def update_existing_peers( self, num_to_remove, peer_table=None, con=None, path=None ):
        """
        Update the set of existing peers:
        * revalidate the existing but old peers
        * remove at most $num_to_remove unhealthy peers

        Return the number of peers removed
        """
        
        # remove peers that are too old
        if self.last_clean_time + atlas_peer_clean_interval() < time_now():
            # remove stale peers
            log.debug("%s: revalidate old peers" % self.my_hostport)
            atlas_revalidate_peers( con=con, path=path )

        # remove a few peers that are unresponsive, and have been talked to a lot
        locked = False
        if peer_table is None:
            locked = True
            peer_table = atlas_peer_table_lock()

        removed = self.remove_unhealthy_peers( num_to_remove, con=con, path=path, peer_table=peer_table )

        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        # if they're also in the new set, remove them there too
        for peer in removed:
            if peer in self.new_peers:
                self.new_peers.remove(peer)

        return len(removed)


    def random_walk_reset( self ):
        """
        Reset the random walk
        """
        self.current_peer = None
        self.prev_peer = None
        self.current_peer_neighbors = []


    def step( self, local_inv=None, peer_table=None, peer_queue=None, con=None, path=None ):
        """
        Execute one round of the peer discovery algorithm:
        * Add at most 10 new peers from the pending peer queue
        (but ping them first, and drop hosts if the pending queue
        gets to be too long).
        * Execute one step of the MHRWDA algorithm.  Add any new
        peers from the neighbor sets discovered.
        * Remove at most 10 old, unresponsive peers from the peer DB.
        """

        if os.environ.get("BLOCKSTACK_TEST", None) == "1":
            log.debug("%s: %s step" % (self.my_hostport, self.__class__.__name__))

        if self.max_neighbors is None:
            self.max_neighbors = atlas_max_neighbors()
            log.debug("%s: max neighbors is %s" % (self.my_hostport, self.max_neighbors))

        current_peers = self.get_current_peers( peer_table=peer_table )

        # add some new peers 
        num_added = self.update_new_peers( 10, current_peers, peer_queue=peer_queue, peer_table=peer_table, con=con, path=path )

        # use MHRWDA to walk the peer graph.
        # first, begin the walk if we haven't already 
        if self.current_peer is None and len(current_peers) > 0:
            
            self.current_peer = current_peers[ random.randint(0,len(current_peers)-1) ]
            
            log.debug("%s: crawl %s" % (self.my_hostport, self.current_peer))
            peer_neighbors = self.get_neighbors( self.current_peer, peer_table=peer_table, path=path, con=con )

            if peer_neighbors is None or len(peer_neighbors) == 0:
                log.debug("%s: no peers from %s" % (self.my_hostport, self.current_peer))

                # try again later
                self.random_walk_reset()

            else:
                # success!
                self.current_peer_neighbors = peer_neighbors

                # don't talk to myself
                if self.my_hostport in self.current_peer_neighbors:
                    self.current_peer_neighbors.remove(self.my_hostport)

                log.debug("%s: neighbors of %s are (%s): %s" % (self.my_hostport, self.current_peer, len(self.current_peer_neighbors), ",".join(self.current_peer_neighbors)))

                # remember to contact these peers later
                self.new_peers = list(set( self.new_peers + peer_neighbors ))

        # can we walk now?
        if self.current_peer is not None:
            
            next_peer, next_peer_neighbors = self.random_walk_graph( self.prev_peer, self.prev_peer_degree, self.current_peer, self.current_peer_neighbors, con=con, path=path, peer_table=peer_table )
            if next_peer is not None and next_peer_neighbors is not None:
                # success!
                self.prev_peer = self.current_peer
                self.prev_peer_degree = len(self.current_peer_neighbors)
                self.current_peer = next_peer
                self.current_peer_neighbors = next_peer_neighbors
                
                # crawl new peers
                self.new_peers = list(set(self.new_peers + self.current_peer_neighbors))

            else:
                log.error("%s: failed to walk from %s" % (self.my_hostport, self.current_peer))
                self.random_walk_reset()


        # update the existing peer info
        num_removed = self.update_existing_peers( 10, con=con, path=path, peer_table=peer_table )
        return num_added, num_removed


    def run(self):
        self.running = True
        while self.running:
            t1 = time_now()
            num_added, num_removed = self.step( path=self.atlasdb_path )
            t2 = time_now()

            if t2 - t1 < 1.0:
                # take a break
                time_sleep( self.my_hostport, self.__class__.__name__, 1.0 - (t2 - t1) )


    def ask_join(self):
        self.running = False


class AtlasHealthChecker( threading.Thread ):
    """
    Thread that continuously tries to refresh zonefile
    inventory information from our neighbor set.
    Also finds unhealthy or old peers and removes them
    from the peer table and peer db.
    """
    def __init__(self, my_host, my_port, path=None):
        threading.Thread.__init__(self)
        self.running = False
        self.path = path
        self.hostport = "%s:%s" % (my_host, my_port)
        self.last_clean_time = 0
        if path is None:
            path = atlasdb_path()
        
        self.atlasdb_path = path

    def step(self, con=None, path=None, peer_table=None, local_inv=None):
        """
        Find peers with stale zonefile inventory data,
        and refresh them.

        Return True on success
        Return False on error
        """
        if os.environ.get("BLOCKSTACK_TEST", None) == "1":
            log.debug("%s: %s step" % (self.hostport, self.__class__.__name__))

        if path is None:
            path = self.path

        peer_hostports = []
        stale_peers = []

        lock = False
        if peer_table is None:
            lock = True
            peer_table = atlas_peer_table_lock()

        num_peers = len(peer_table.keys())
        peer_hostports = peer_table.keys()[:]

        # who are we going to ping?
        # someone we haven't pinged in a while, chosen at random
        log.debug("Refresh zonefile inventories for at most %s peers" % num_peers)
        for peer in peer_hostports:
            if not atlas_peer_has_fresh_zonefile_inventory( peer, local_inv=local_inv, peer_table=peer_table, con=con, path=path ):
                # haven't talked to this peer in a while
                stale_peers.append(peer)
                log.debug("Peer %s has a stale zonefile inventory" % peer)

        if lock:
            atlas_peer_table_unlock()
            peer_table = None

        for peer_hostport in stale_peers:
            # refresh everyone
            log.debug("%s: Refresh zonefile inventory for %s" % (self.hostport, peer_hostport))
            res = atlas_peer_refresh_zonefile_inventory( self.hostport, peer_hostport, 0, con=con, path=path, peer_table=peer_table, local_inv=local_inv )
            if res is None:
                log.warning("Failed to refresh zonefile inventory for %s" % peer_hostport)
        
        return 


    def run(self, peer_table=None):
        """
        Loop forever, pinging someone every pass.
        """
        self.running = True
        while self.running:
            local_inv = atlas_get_zonefile_inventory()
            t1 = time_now()
            self.step( peer_table=peer_table, local_inv=local_inv, path=self.atlasdb_path )
            t2 = time_now()

            # don't go too fast 
            if t2 - t1 < 1.0:
                time_sleep( self.hostport, self.__class__.__name__, 1.0 - (t2 - t1) )


    def ask_join(self):
        self.running = False


class AtlasZonefileCrawler( threading.Thread ):
    """
    Thread that continuously tries to find 
    zonefiles that we don't have.
    """

    def __init__(self, my_host, my_port, zonefile_storage_drivers=[], path=None, zonefile_dir=None):
        threading.Thread.__init__(self)
        self.running = False
        self.hostport = "%s:%s" % (my_host, my_port)
        self.path = path 
        self.zonefile_storage_drivers = zonefile_storage_drivers
        self.zonefile_dir = zonefile_dir
        if self.path is None:
            self.path = atlasdb_path()


    def store_zonefile( self, fetched_zfhash, zonefile, peer_hostport, con, path ):
        """
        Store the fetched zonefile to storage and cache it locally.
        Update internal state to mark it present
        Return True on success
        Return False on error
        """
        rc = store_zonefile_to_storage( zonefile, required=self.zonefile_storage_drivers, cache=True, zonefile_dir=self.zonefile_dir )
        if not rc:
            log.error("%s: Failed to store zonefile %s" % (self.hostport, fetched_zfhash))

        else:
            # stored! remember it
            log.debug("%s: got %s from %s" % (self.hostport, fetched_zfhash, peer_hostport))

            # update internal state
            atlasdb_set_zonefile_present( fetched_zfhash, True, con=con, path=path )

        return rc


    def step(self, con=None, path=None, peer_table=None):
        """
        Run one step of this algorithm:
        * find the set of missing zonefiles
        * try to fetch each of them
        * store them
        * update our zonefile database

        Fetch rarest zonefiles first, but batch
        whenever possible.

        Return the number of zonefiles fetched
        """

        if os.environ.get("BLOCKSTACK_TEST", None) == "1":
            log.debug("%s: %s step" % (self.hostport, self.__class__.__name__))

        if path is None:
            path = self.path

        close = False
        if con is None:
            close = True
            con = atlasdb_open( path )
            assert con is not None

        num_fetched = 0
        locked = False

        if peer_table is None:
            locked = True
            peer_table = atlas_peer_table_lock()

        missing_zfinfo = atlas_find_missing_zonefile_availability( peer_table=peer_table, con=con, path=path )
        peer_hostports = peer_table.keys()[:]

        if locked:
            atlas_peer_table_unlock()
            peer_table = None

        # ask for zonefiles in rarest-first order
        zonefile_ranking = [ (missing_zfinfo[zfhash]['popularity'], zfhash) for zfhash in missing_zfinfo.keys() ]
        zonefile_ranking.sort()
        zonefile_hashes = list(set([zfhash for (_, zfhash) in zonefile_ranking]))
        zonefile_origins = {}   # map peer hostport to list of zonefile hashes

        # which peers can serve each zonefile?
        for zfhash in missing_zfinfo.keys():
            for peer_hostport in peer_hostports:
                if not zonefile_origins.has_key(peer_hostport):
                    zonefile_origins[peer_hostport] = []

                if peer_hostport in missing_zfinfo[zfhash]['peers']:
                    zonefile_origins[peer_hostport].append( zfhash )

        log.debug("%s: missing %s zonefiles" % (self.hostport, len(zonefile_hashes)))

        while len(zonefile_hashes) > 0:

            zfhash = zonefile_hashes[0]
            peers = missing_zfinfo[zfhash]['peers']

            # is this zonefile available via storage?
            if not missing_zfinfo[zfhash]['tried_storage']:
                log.debug("Try loading %s from storage" % zfhash)

                zonefile_info = atlas_get_zonefile_from_storage( zfhash, self.zonefile_storage_drivers )

                # tried loading from storage
                atlasdb_set_zonefile_tried_storage( zfhash, True, con=con, path=path )

                if 'error' in zonefile_info:
                    log.error("%s: Failed to get zonefile '%s' from storage" % (self.hostport, zfhash))

                else:
                    # got it! remember it
                    log.debug("%s: got %s from storage" % (self.hostport, zfhash))
                    rc = self.store_zonefile( zfhash, zonefile_info['zonefile'], "storage", con, path )
                    if rc:
                        # don't ask for it again
                        zonefile_hashes.pop(0)
                        num_fetched += 1
                        continue

            if len(peers) == 0:
                log.debug("%s: zonefile %s is unavailable" % (self.hostport, zfhash))
                zonefile_hashes.pop(0)
                continue

            # try this zonefile's hosts in order by perceived availability
            peers = atlas_rank_peers_by_health( peer_list=peers, with_zero_requests=True )
            log.debug("%s: zonefile %s available from %s peers (%s...)" % (self.hostport, zfhash, len(peers), ",".join(peers)[:min(5, len(peers))]))

            for peer_hostport in peers:

                # what other zonefiles can we get?
                # only ask for the ones we don't have
                peer_zonefile_hashes = []
                for zfh in zonefile_origins[peer_hostport]:
                    if zfh in zonefile_hashes:
                        # can ask for this one too
                        peer_zonefile_hashes.append( zfh )

                if len(peer_zonefile_hashes) == 0:
                    log.debug("%s: No zonefiles available from %s" % (self.hostport, peer_hostport))
                    continue

                # get them all
                log.debug("%s: get %s zonefiles from %s" % (self.hostport, len(peer_zonefile_hashes), peer_hostport))
                zonefiles = atlas_get_zonefiles( self.hostport, peer_hostport, peer_zonefile_hashes, peer_table=peer_table )
                if zonefiles is not None:

                    # got zonefiles!
                    for fetched_zfhash, zonefile_txt in zonefiles.items():
                        
                        zonefile = blockstack_zones.parse_zone_file( zonefile_txt )
                        if fetched_zfhash not in peer_zonefile_hashes:
                            # unsolicited
                            log.warn("%s: Unsolicited zonefile %s" % (self.hostport, fetched_zfhash))
                            continue

                        rc = self.store_zonefile( fetched_zfhash, zonefile, peer_hostport, con, path )
                        if rc:
                            # don't ask for it again
                            peer_zonefile_hashes.remove(fetched_zfhash)
                            num_fetched += 1
                
                else:
                    log.debug("%s: no data received from %s" % (self.hostport, peer_hostport))

                if locked:
                    peer_table = atlas_peer_table_lock()

                # if the node didn't actually have these zonefiles, then 
                # update their inventories so we don't ask for them again.
                for zfhash in peer_zonefile_hashes:
                    log.debug("%s: %s did not have %s" % (self.hostport, peer_hostport, zfhash))
                    atlas_peer_set_zonefile_status( peer_hostport, zfhash, False, zonefile_bits=missing_zfinfo[zfhash]['indexes'], peer_table=peer_table )

                if locked:
                    atlas_peer_table_unlock()
                    peer_table = None
           
            # done with this zonefile
            zonefile_hashes.pop(0)

        if close:
            con.close()

        log.debug("%s: fetched %s zonefiles" % (self.hostport, num_fetched))
        return num_fetched

    
    def run(self):
        self.running = True
        while self.running:
            con = atlasdb_open( self.path )

            t1 = time.time()
            self.step( con=con, path=self.path )
            t2 = time.time()

            con.close()
            
            if t2 - t1 < 1.0:
                time_sleep( self.hostport, self.__class__.__name__, 1.0 - (t2 - t1) )


    def ask_join(self):
        self.running = False



class AtlasZonefilePusher(threading.Thread):
    """
    Continuously drain the queue of zonefiles
    we can push, by sending them off to 
    known peers who need them.
    """
    def __init__(self, host, port, path=None ):
        threading.Thread.__init__(self)
        self.host = host
        self.port = port
        self.hostport = "%s:%s" % (host, port)
        self.path = path
        if self.path is None:
            self.path = atlasdb_path()

        self.push_timeout = None


    def step( self, peer_table=None, zonefile_queue=None, path=None ):
        """
        Run one step of this algorithm.
        Push the zonefile to all the peers that need it.
        Return the number of peers we sent to
        """
       
        if os.environ.get("BLOCKSTACK_TEST", None) == "1":
            log.debug("%s: %s step" % (self.hostport, self.__class__.__name__))

        if self.push_timeout is None:
            self.push_timeout = atlas_push_zonefiles_timeout()

        zfinfo = atlas_zonefile_push_dequeue( zonefile_queue=zonefile_queue )
        if zfinfo is None:
            return 0

        zfhash = zfinfo.keys()[0]
        zfdata_txt = zfinfo[zfhash]
        zfdata = None

        try:
            zfdata = blockstack_zones.parse_zone_file( zfdata_txt )
        except Exception, e:
            log.exception(e)
            log.error("Failed to parse zonefile %s" % zfhash)
            return 0

        zfbits = atlasdb_get_zonefile_bits( zfhash, path=path )
        if len(zfbits) == 0:
            # nope 
            return 0

        # see if we can send this somewhere
        table_locked = False
        if peer_table is None:
            peer_table = atlas_peer_table_lock()
            table_locked = True

        peers = atlas_zonefile_find_push_peers( zfhash, peer_table=peer_table, zonefile_bits=zfbits )

        if table_locked:
            atlas_peer_table_unlock()
            peer_table = None

        if len(peers) == 0:
            # everyone has it
            log.debug("%s: All peers have zonefile %s" % (self.hostport, zfhash))
            return 0

        # push it off
        ret = 0
        for peer in peers:
            log.debug("%s: Push to %s" % (self.hostport, peer))
            atlas_zonefile_push( self.hostport, peer, zfdata, timeout=self.push_timeout )
            ret += 1

        return ret

    
    def run(self):
        self.running = True
        while self.running:
            t1 = time_now()
            num_pushed = self.step( path=self.path )
            t2 = time_now()
            if t2 - t1 < 1.0:
                time_sleep(self.hostport, self.__class__.__name__, 1.0 - (t2 - t1))
                pass
       

    def ask_join(self):
        self.running = False



def atlas_node_start( my_hostname, my_portnum, atlasdb_path=None, zonefile_dir=None, zonefile_storage_drivers=[] ):
    """
    Start up the atlas node.
    Return a bundle of atlas state
    """
    atlas_state = {}
    atlas_state['peer_crawler'] = AtlasPeerCrawler( my_hostname, my_portnum )
    atlas_state['health_checker'] = AtlasHealthChecker( my_hostname, my_portnum, path=atlasdb_path )
    atlas_state['zonefile_crawler'] = AtlasZonefileCrawler( my_hostname, my_portnum, zonefile_storage_drivers=zonefile_storage_drivers, path=atlasdb_path, zonefile_dir=zonefile_dir )
    atlas_state['zonefile_pusher'] = AtlasZonefilePusher( my_hostname, my_portnum, path=atlasdb_path )

    # start them all up
    for component in atlas_state.keys():
        log.debug("Starting Atlas component '%s'" % component)
        atlas_state[component].start()

    return atlas_state


def atlas_node_stop( atlas_state ):
    """
    Stop the atlas node threads
    """
    for component in atlas_state.keys():
        log.debug("Stopping Atlas component '%s'" % component)
        atlas_state[component].ask_join()
        atlas_state[component].join()

    return True
