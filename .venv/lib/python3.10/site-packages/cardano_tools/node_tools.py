import signal
import logging
import subprocess


class CardanoNodeError(Exception):
    pass


class CardanoNode:
    """Provides an interface for starting up and shutting down a Cardano node."""

    def __init__(
        self,
        binary,
        topology,
        database_path,
        socket_path,
        config,
        port=3001,
        host_addr=None,
        kes_key=None,
        vrf_key=None,
        cert=None,
        show_output=False,
    ):
        self.logger = logging.getLogger(__name__)
        self.binary = binary
        self.topology = topology
        self.db_path = database_path
        self.socket_path = socket_path
        self.port = port
        self.config = config
        self.host_addr = host_addr
        self.kes_key = kes_key
        self.vrf_key = vrf_key
        self.cert = cert
        self.process = None
        self.show_output = show_output

    def __exec(self, args):
        self.logger.debug(f'CMD: "{args}"')
        if self.show_output:
            self.process = subprocess.Popen(args.split())
        else:
            self.process = subprocess.Popen(
                args.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )

    def start(self, mode="relay"):
        """Start the cardano-node (default relay mode)."""
        cmd = (
            f"{self.binary} run "
            f"--topology {self.topology} "
            f"--database-path {self.db_path} "
            f"--socket-path {self.socket_path} "
            f"--port {self.port} "
            f"--config {self.config} "
        )
        if self.host_addr is not None:
            cmd += f"--host-addr {self.host_addr} "
        if mode.lower() == "pool":
            cmd += (
                f"--shelley-kes-key {self.kes_key} "
                f"--shelley-vrf-key {self.vrf_key} "
                f"--shelley-operational-certificate {self.cert}"
            )
        self.__exec(cmd)

    def stop(self):
        """Stop the cardano-node (send SIGINT)."""
        self.process.send_signal(signal.SIGINT)
