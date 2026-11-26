"""Unified multi-process service orchestrator for India Post AI Shipment Assistant.

Supervises the FastAPI web server, proactive monitoring scheduler, and
outbox notification delivery worker with graceful lifecycle management,
signal propagation, and health monitoring.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] [orchestrator] %(message)s",
)
logger = logging.getLogger("orchestrator")


class ServiceProcess:
    def __init__(self, name: str, cmd: list[str]) -> None:
        self.name = name
        self.cmd = cmd
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        logger.info("Starting service: %s (%s)", self.name, " ".join(self.cmd))
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(BASE_DIR),
            env=os.environ.copy(),
        )

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 5.0) -> None:
        if self.proc and self.proc.poll() is None:
            logger.info("Stopping service: %s (PID %s)...", self.name, self.proc.pid)
            try:
                self.proc.terminate()
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning("Service %s did not terminate gracefully; killing.", self.name)
                self.proc.kill()
                self.proc.wait()


class ServiceOrchestrator:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        enable_monitoring: bool = True,
        enable_notifications: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.enable_monitoring = enable_monitoring
        self.enable_notifications = enable_notifications
        self.services: list[ServiceProcess] = []
        self._running = False

    def build_services(self) -> list[ServiceProcess]:
        python_bin = sys.executable
        services = []

        # 1. FastAPI Web Server via Uvicorn
        web_cmd = [
            python_bin,
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--log-level",
            os.getenv("LOG_LEVEL", "info").lower(),
        ]
        services.append(ServiceProcess("web-server", web_cmd))

        # 2. Proactive Monitoring Scheduler
        if self.enable_monitoring:
            monitoring_cmd = [python_bin, "run_monitoring_scheduler.py"]
            services.append(ServiceProcess("monitoring-scheduler", monitoring_cmd))

        # 3. Notification Delivery Scheduler
        if self.enable_notifications:
            notification_cmd = [python_bin, "run_notification_scheduler.py"]
            services.append(ServiceProcess("notification-scheduler", notification_cmd))

        return services

    def start(self) -> None:
        self.services = self.build_services()
        self._running = True

        logger.info("Initializing India Post Assistant multi-service orchestrator...")
        for svc in self.services:
            svc.start()

        def handle_signal(signum, _frame):
            logger.info("Received signal %s; shutting down orchestrator...", signum)
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        logger.info("All services running. Press Ctrl+C to terminate.")

        try:
            while self._running:
                for svc in self.services:
                    if not svc.is_alive():
                        exit_code = svc.proc.returncode if svc.proc else -1
                        logger.error("Service %s exited unexpectedly with code %s!", svc.name, exit_code)
                        self.stop()
                        sys.exit(exit_code or 1)
                time.sleep(1.0)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received; stopping services...")
            self.stop()

    def stop(self) -> None:
        self._running = False
        for svc in reversed(self.services):
            svc.stop()
        logger.info("Orchestrator shutdown complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="India Post AI Shipment Assistant Service Orchestrator")
    parser.add_argument("--host", default="127.0.0.1", help="Web host interface")
    parser.add_argument("--port", type=int, default=8000, help="Web port")
    parser.add_argument("--no-monitoring", action="store_true", help="Disable monitoring scheduler")
    parser.add_argument("--no-notifications", action="store_true", help="Disable notification scheduler")
    parser.add_argument("--check-only", action="store_true", help="Run diagnostics check and exit")

    args = parser.parse_args()

    if args.check_only:
        from scripts.health_inspector import main as run_diagnostics
        run_diagnostics()
        return

    orchestrator = ServiceOrchestrator(
        host=args.host,
        port=args.port,
        enable_monitoring=not args.no_monitoring,
        enable_notifications=not args.no_notifications,
    )
    orchestrator.start()


if __name__ == "__main__":
    main()
