"""Config schema + loader for valr-cm-spot."""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class GlobalConfig:
    instance_name: str = "valr-cm-spot"
    account_a_id: str = ""
    account_b_id: str = ""
    account_a_name: str = "CMS1"
    account_b_name: str = "CMS2"
    rate_limit_per_sec: int = 80
    summary_interval_seconds: int = 300
    rebalance_interval_cycles: int = 6
    rebalance_threshold_pct: float = 0.60
    min_transfer_value_usd: float = 1.0
    external_fill_alert_threshold: float = 0.10  # 10% external triggers alert
    leak_window_size: int = 100
    leak_min_samples: int = 20
    alert_cooldown_seconds: int = 1800
    stagger_seconds: float = 1.5
    use_account_ws: bool = True
    telegram_chat_id: str = ""
    cancel_all_on_startup: bool = True


@dataclass
class PairDefaults:
    cycle_interval_seconds: float = 15.0
    min_spread_ticks: int = 2
    max_spread_bps: int = 200
    print_value_usd_min: float = 1.50
    print_value_usd_max: float = 4.00
    inventory_floor_usd: float = 5.00
    max_consecutive_same_maker: int = 5
    quote_to_usd: float = 1.0  # conversion factor from pair quote -> USD


@dataclass
class PairConfig:
    enabled: bool = False
    cycle_interval_seconds: Optional[float] = None
    min_spread_ticks: Optional[int] = None
    max_spread_bps: Optional[int] = None
    print_value_usd_min: Optional[float] = None
    print_value_usd_max: Optional[float] = None
    inventory_floor_usd: Optional[float] = None
    max_consecutive_same_maker: Optional[int] = None
    quote_to_usd: Optional[float] = None


@dataclass
class Config:
    global_: GlobalConfig = field(default_factory=GlobalConfig)
    defaults: PairDefaults = field(default_factory=PairDefaults)
    pairs: Dict[str, PairConfig] = field(default_factory=dict)

    def for_pair(self, pair: str) -> PairDefaults:
        """Resolve effective config for a pair (defaults overlaid by per-pair overrides)."""
        pc = self.pairs.get(pair, PairConfig())
        d = self.defaults
        return PairDefaults(
            cycle_interval_seconds=pc.cycle_interval_seconds if pc.cycle_interval_seconds is not None else d.cycle_interval_seconds,
            min_spread_ticks=pc.min_spread_ticks if pc.min_spread_ticks is not None else d.min_spread_ticks,
            max_spread_bps=pc.max_spread_bps if pc.max_spread_bps is not None else d.max_spread_bps,
            print_value_usd_min=pc.print_value_usd_min if pc.print_value_usd_min is not None else d.print_value_usd_min,
            print_value_usd_max=pc.print_value_usd_max if pc.print_value_usd_max is not None else d.print_value_usd_max,
            inventory_floor_usd=pc.inventory_floor_usd if pc.inventory_floor_usd is not None else d.inventory_floor_usd,
            max_consecutive_same_maker=pc.max_consecutive_same_maker if pc.max_consecutive_same_maker is not None else d.max_consecutive_same_maker,
            quote_to_usd=pc.quote_to_usd if pc.quote_to_usd is not None else d.quote_to_usd,
        )


def load(path: Path | str) -> Config:
    raw = _json.loads(Path(path).read_text())
    g = raw.get("global", {})
    d = raw.get("defaults", {})
    pairs_raw = raw.get("pairs", {})
    cfg = Config(
        global_=GlobalConfig(**{k: v for k, v in g.items() if k in GlobalConfig.__dataclass_fields__}),
        defaults=PairDefaults(**{k: v for k, v in d.items() if k in PairDefaults.__dataclass_fields__}),
        pairs={
            sym: PairConfig(**{k: v for k, v in pc.items() if k in PairConfig.__dataclass_fields__})
            for sym, pc in pairs_raw.items()
        },
    )
    return cfg


def enabled_pairs(cfg: Config) -> list[str]:
    return [p for p, pc in cfg.pairs.items() if pc.enabled]
