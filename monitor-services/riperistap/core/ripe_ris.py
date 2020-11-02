import os
import time
from copy import deepcopy

import pytricia
import redis
import requests
import ujson as json
from artemis_utils import configure_data_task
from artemis_utils import ControlHandler
from artemis_utils import get_ip_version
from artemis_utils import get_logger
from artemis_utils import HealthHandler
from artemis_utils import key_generator
from artemis_utils import mformat_validator
from artemis_utils import normalize_msg_path
from artemis_utils import ping_redis
from artemis_utils import RABBITMQ_URI
from artemis_utils import REDIS_HOST
from artemis_utils import REDIS_PORT
from artemis_utils import search_worst_prefix
from artemis_utils import stop_data_task
from artemis_utils import translate_rfc2622
from artemis_utils.rabbitmq_util import create_exchange
from kombu import Connection
from kombu import Producer
from tornado.ioloop import IOLoop
from tornado.web import Application
from tornado.web import RequestHandler

log = get_logger()
update_to_type = {"announcements": "A", "withdrawals": "W"}
update_types = ["announcements", "withdrawals"]
redis = redis.Redis(host=REDIS_HOST, port=REDIS_PORT)
DEFAULT_MON_TIMEOUT_LAST_BGP_UPDATE = 60 * 60
# TODO: add the following in utils
REST_PORT = 3000
CONFIGURATION_HOST = "configuration"


def configure_ripe_ris(msg):
    try:
        monitors = msg.get("monitors", {})
        rules = msg.get("rules", [])

        # check if "riperis" is configured at all
        if "riperis" not in monitors:
            stop_data_task()
            return {"success": True, "message": "data_task not in configuration"}

        # calculate ripe ris hosts
        hosts = set(monitors["riperis"])
        if hosts == set("."):
            hosts = set()

        # calculate all configured prefixes
        prefix_tree = {"v4": pytricia.PyTricia(32), "v6": pytricia.PyTricia(128)}
        for rule in rules:
            for prefix in rule["prefixes"]:
                for translated_prefix in translate_rfc2622(prefix):
                    ip_version = get_ip_version(translated_prefix)
                    prefix_tree[ip_version].insert(translated_prefix, "")

        # only keep super prefixes for monitoring
        prefixes = set()
        for ip_version in prefix_tree:
            for prefix in prefix_tree[ip_version]:
                worst_prefix = search_worst_prefix(prefix, prefix_tree[ip_version])
                if worst_prefix:
                    prefixes.add(worst_prefix)

        # configure the data_task
        configure_data_task(RipeRisTap, prefixes=prefixes, hosts=hosts)
        return {"success": True, "message": "configured"}

    except Exception:
        log.exception("Exception")
        return {"success": False, "message": "error during data_task configuration"}


class ConfigHandler(RequestHandler):
    def post(self):
        try:
            msg = json.loads(self.request.body)
            self.write(configure_ripe_ris(msg))
        except Exception:
            self.write(
                {"success": False, "message": "error during data_task configuration"}
            )


class RipeRisTap:
    def __init__(self, **kwargs):
        self._running = False
        self.prefixes = kwargs["prefixes"]
        self.hosts = kwargs["hosts"]

    def is_running(self):
        return self._running

    def stop(self):
        self._running = False

    def run(self):
        self._running = True
        ping_redis(redis)
        redis.set(
            "ris_seen_bgp_update",
            "1",
            ex=int(
                os.getenv(
                    "MON_TIMEOUT_LAST_BGP_UPDATE", DEFAULT_MON_TIMEOUT_LAST_BGP_UPDATE
                )
            ),
        )
        with Connection(RABBITMQ_URI) as connection:
            update_exchange = create_exchange("bgp-update", connection, declare=True)
            prefix_tree = {"v4": pytricia.PyTricia(32), "v6": pytricia.PyTricia(128)}
            for prefix in self.prefixes:
                ip_version = get_ip_version(prefix)
                prefix_tree[ip_version].insert(prefix, "")

            ris_suffix = os.getenv("RIS_ID", "my_as")

            validator = mformat_validator()
            with Producer(connection) as producer:
                while self._running:
                    try:
                        events = requests.get(
                            "https://ris-live.ripe.net/v1/stream/?format=json&client=artemis-{}".format(
                                ris_suffix
                            ),
                            stream=True,
                            timeout=10,
                        )
                        # http://docs.python-requests.org/en/latest/user/advanced/#streaming-requests
                        iterator = events.iter_lines()
                        next(iterator)
                        for data in iterator:
                            if not self._running:
                                break
                            try:
                                parsed = json.loads(data)
                                msg = parsed["data"]
                                if "type" in parsed and parsed["type"] == "ris_error":
                                    log.error(msg)
                                # also check if ris host is in the configuration
                                elif (
                                    "type" in msg
                                    and msg["type"] == "UPDATE"
                                    and (not self.hosts or msg["host"] in self.hosts)
                                ):
                                    norm_ris_msgs = self.normalize_ripe_ris(
                                        msg, prefix_tree
                                    )
                                    for norm_ris_msg in norm_ris_msgs:
                                        redis.set(
                                            "ris_seen_bgp_update",
                                            "1",
                                            ex=int(
                                                os.getenv(
                                                    "MON_TIMEOUT_LAST_BGP_UPDATE",
                                                    DEFAULT_MON_TIMEOUT_LAST_BGP_UPDATE,
                                                )
                                            ),
                                        )
                                        try:
                                            if validator.validate(norm_ris_msg):
                                                norm_path_msgs = normalize_msg_path(
                                                    norm_ris_msg
                                                )
                                                for norm_path_msg in norm_path_msgs:
                                                    key_generator(norm_path_msg)
                                                    log.debug(norm_path_msg)
                                                    producer.publish(
                                                        norm_path_msg,
                                                        exchange=update_exchange,
                                                        routing_key="update",
                                                        serializer="ujson",
                                                    )
                                            else:
                                                log.warning(
                                                    "Invalid format message: {}".format(
                                                        msg
                                                    )
                                                )
                                        except BaseException:
                                            log.exception(
                                                "Error when normalizing BGP message: {}".format(
                                                    norm_ris_msg
                                                )
                                            )
                            except Exception:
                                log.exception("exception message {}".format(data))
                        if not self._running:
                            log.warning(
                                "Iterator ran out of data; the connection will be retried"
                            )
                    except Exception:
                        log.info(
                            "RIPE RIS Server closed connection. Restarting socket in 60seconds.."
                        )
                        time.sleep(60)

    @staticmethod
    def normalize_ripe_ris(msg, prefix_tree):
        msgs = []
        if isinstance(msg, dict):
            msg["key"] = None  # initial placeholder before passing the validator
            if "community" in msg:
                msg["communities"] = [
                    {"asn": comm[0], "value": comm[1]} for comm in msg["community"]
                ]
                del msg["community"]
            if "host" in msg:
                msg["service"] = "ripe-ris|" + msg["host"]
                del msg["host"]
            if "peer_asn" in msg:
                msg["peer_asn"] = int(msg["peer_asn"])
            if "path" not in msg:
                msg["path"] = []
            if "timestamp" in msg:
                msg["timestamp"] = float(msg["timestamp"])
            if "type" in msg:
                del msg["type"]
            if "raw" in msg:
                del msg["raw"]
            if "origin" in msg:
                del msg["origin"]
            if "id" in msg:
                del msg["id"]
            if "announcements" in msg and "withdrawals" in msg:
                # need 2 separate messages
                # one for announcements
                msg_ann = deepcopy(msg)
                msg_ann["type"] = update_to_type["announcements"]
                prefixes = []
                for element in msg_ann["announcements"]:
                    if "prefixes" in element:
                        prefixes.extend(element["prefixes"])
                for prefix in prefixes:
                    ip_version = get_ip_version(prefix)
                    try:
                        if prefix in prefix_tree[ip_version]:
                            new_msg = deepcopy(msg_ann)
                            new_msg["prefix"] = prefix
                            del new_msg["announcements"]
                            del new_msg["withdrawals"]
                            msgs.append(new_msg)
                    except Exception:
                        log.exception("exception")
                # one for withdrawals
                msg_wit = deepcopy(msg)
                msg_wit["type"] = update_to_type["withdrawals"]
                msg_wit["path"] = []
                msg_wit["communities"] = []
                prefixes = msg_wit["withdrawals"]
                for prefix in prefixes:
                    ip_version = get_ip_version(prefix)
                    try:
                        if prefix in prefix_tree[ip_version]:
                            new_msg = deepcopy(msg_wit)
                            new_msg["prefix"] = prefix
                            del new_msg["announcements"]
                            del new_msg["withdrawals"]
                            msgs.append(new_msg)
                    except Exception:
                        log.exception("exception")
            else:
                for update_type in update_types:
                    if update_type in msg:
                        msg["type"] = update_to_type[update_type]
                        prefixes = []
                        for element in msg[update_type]:
                            if update_type == "announcements":
                                if "prefixes" in element:
                                    prefixes.extend(element["prefixes"])
                            elif update_type == "withdrawals":
                                prefixes.append(element)
                        for prefix in prefixes:
                            ip_version = get_ip_version(prefix)
                            try:
                                if prefix in prefix_tree[ip_version]:
                                    new_msg = deepcopy(msg)
                                    new_msg["prefix"] = prefix
                                    del new_msg[update_type]
                                    msgs.append(new_msg)
                            except Exception:
                                log.exception("exception")
        return msgs


def make_app():
    return Application(
        [
            ("/config", ConfigHandler),
            ("/control", ControlHandler),
            ("/health", HealthHandler),
        ]
    )


if __name__ == "__main__":
    # ask for initial configuration
    r = requests.get("http://{}:{}/config".format(CONFIGURATION_HOST, REST_PORT))
    conf_res = configure_ripe_ris(r.json())
    assert conf_res["success"], conf_res["message"]
    app = make_app()
    app.listen(REST_PORT)
    log.info("Listening to port {}".format(REST_PORT))
    IOLoop.current().start()
