"""Monitor loop — main entry point for anomaly scanning and repair."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from breadforge.monitor.anomaly import AnomalyStore
from breadforge.monitor.detect import _detect_anomalies
from breadforge.monitor.repair import _repair_agent, _repair_auto

if TYPE_CHECKING:
    from breadforge.beads.store import BeadStore
    from breadforge.config import Config
    from breadforge.logger import Logger


async def run_monitor(
    store: BeadStore,
    config: Config,
    logger: Logger,
    *,
    once: bool = False,
    interval_seconds: int = 300,
    dry_run: bool = False,
    max_repair_attempts: int = 3,
) -> None:
    """Main monitor loop. Detects anomalies and dispatches repairs."""
    astore = AnomalyStore(config.beads_dir, config.repo)

    while True:
        logger.info("monitor: scanning for anomalies")

        new_anomalies = _detect_anomalies(store, config.repo)

        existing_by_kind_issue = {(a.kind, a.issue_number): a for a in astore.list_open()}

        for anomaly in new_anomalies:
            key = (anomaly.kind, anomaly.issue_number)
            if key in existing_by_kind_issue:
                continue
            logger.anomaly(anomaly.anomaly_id, anomaly.kind, anomaly.issue_number)
            astore.write(anomaly)

        for abead in astore.list_open():
            if abead.repair_attempts >= max_repair_attempts:
                logger.error(
                    f"anomaly {abead.anomaly_id} exceeded max repair attempts — needs human review",
                    anomaly_id=abead.anomaly_id,
                )
                continue

            if dry_run:
                logger.info(f"[dry-run] would repair {abead.kind} anomaly {abead.anomaly_id}")
                continue

            if abead.repair_tier == "auto":
                await _repair_auto(abead, config.repo, logger)
            elif abead.repair_tier == "agent":
                await _repair_agent(abead, store, config, logger)

            astore.write(abead)

        if once:
            break

        await asyncio.sleep(interval_seconds)
