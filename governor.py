#!/usr/bin/env python

import sys, os, yaml, time, urllib2, atexit, ssl, signal
import logging

from helpers.etcd import Etcd
from helpers.postgresql import Postgresql
from helpers.ha import Ha
import socket

LOG_LEVEL = logging.DEBUG if os.getenv('DEBUG', None) else logging.INFO

logging.basicConfig(format='%(asctime)s %(levelname)s: %(message)s', level=LOG_LEVEL)


# stop postgresql on script exit
def stop_postgresql(postgresql):
    postgresql.stop()

# wait for etcd to be available
def wait_for_etcd(message, etcd, postgresql):
    etcd_ready = False
    while not etcd_ready:
        try:
            etcd.touch_member(postgresql.name, postgresql.connection_string)
            etcd_ready = True
        except (urllib2.URLError, ssl.SSLError, socket.timeout) as e:
            logging.info(e)
            logging.info("waiting on etcd: %s" % message)
            time.sleep(5)

def signalhandler(signum, frame):
    print('Received ', signum)
    stop_postgresql(postgresql)

def run(config):
    etcd = Etcd(config["etcd"])
    postgresql = Postgresql(config["postgresql"])
    ha = Ha(postgresql, etcd)

    atexit.register(stop_postgresql, postgresql)
    signal.signal(signal.SIGTERM, signalhandler)
    logging.info("Governor Starting up")
# is data directory empty?
    if postgresql.data_directory_empty():
        logging.info("Governor Starting up: Empty Data Dir")
        # racing to initialize
        wait_for_etcd("cannot initialize member without ETCD", etcd, postgresql)
        if etcd.race("/initialize", postgresql.name):
            logging.info("Governor Starting up: Initialisation Race ... WON!!!")
            logging.info("Governor Starting up: Initialise Postgres")
            postgresql.initialize()
            logging.info("Governor Starting up: Initialise Complete")
            etcd.take_leader(postgresql.name)
            logging.info("Governor Starting up: Starting Postgres")
            postgresql.start()
        else:
            logging.info("Governor Starting up: Initialisation Race ... LOST")
            logging.info("Governor Starting up: Sync Postgres from Leader")
            synced_from_leader = False
            while not synced_from_leader:
                leader = etcd.current_leader()
                if not leader:
                    time.sleep(5)
                    continue
                if postgresql.sync_from_leader(leader):
                    logging.info("Governor Starting up: Sync Completed")
                    postgresql.write_recovery_conf(leader)
                    logging.info("Governor Starting up: Starting Postgres")
                    postgresql.start()
                    synced_from_leader = True
                else:
                    time.sleep(5)
    else:
        logging.info("Governor Starting up: Existing Data Dir")
        postgresql.follow_no_leader()
        logging.info("Governor Starting up: Starting Postgres")
        postgresql.start()

    wait_for_etcd("running in readonly mode; cannot participate in cluster HA without etcd", etcd, postgresql)
    logging.info("Governor Running: Starting Running Loop")
    while True:
        try:
            logging.info("Governor Running: %s" % ha.run_cycle())

            # create replication slots
            if postgresql.is_leader():
                logging.info("Governor Running: I am the Leader")
            for node in etcd.members():
                member = node["hostname"]
                if member != postgresql.name:
                    if postgresql.is_leader():
                        postgresql.ensure_replication_slot(
                            postgresql.replication_slot_name(member)
                        )
                    else:
                        postgresql.drop_replication_slot(
                            postgresql.replication_slot_name(member)
                        )
            etcd.touch_member(postgresql.name, postgresql.connection_string)

            time.sleep(config["loop_wait"])
        except (urllib2.URLError, socket.timeout):
            logging.info("Lost connection to etcd, setting no leader and waiting on etcd")
            postgresql.follow_no_leader()
            wait_for_etcd("running in readonly mode; cannot participate in cluster HA without etcd", etcd, postgresql)

if __name__ == "__main__":
    f = open(sys.argv[1], "r")
    config = yaml.load(f.read())
    f.close()

    run(config)
