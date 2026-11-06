from dataclasses import dataclass


@dataclass(frozen=True)
class EngineConfig:
    """Runtime policy for the balanced, hybrid signal engine.

    Thresholds are initial engineering defaults, not claims of profitability.
    They must be validated with historical replay before live alerting.
    """
    min_score: float = 65.0
    min_adx: float = 22.0
    max_entry_extension_atr: float = 1.25
    min_risk_reward: float = 1.5
    max_candidate_age_bars: int = 2
    min_history_bars: int = 220
    allow_intrabar_candidates: bool = True
    allow_closed_bar_candidates: bool = True
    max_alerts_per_underlying_bar: int = 1
    conflict_score_margin: float = 8.0
    require_valid_stop: bool = True
    require_closed_htf: bool = True
    unified_scoring_enabled: bool = True

    def __post_init__(self):
        if not 0 <= self.min_score <= 100:
            raise ValueError("min_score must be between 0 and 100")
        if self.min_adx < 22.0:
            raise ValueError("min_adx cannot be lower than the production floor of 22")
        if self.max_entry_extension_atr <= 0:
            raise ValueError("max_entry_extension_atr must be positive")
        if self.min_risk_reward < 0:
            raise ValueError("min_risk_reward cannot be negative")
        if self.min_history_bars < 220:
            raise ValueError("min_history_bars must preserve the 220-bar production baseline")
        if self.max_candidate_age_bars < 0:
            raise ValueError("max_candidate_age_bars cannot be negative")
        if self.max_alerts_per_underlying_bar < 1:
            raise ValueError("max_alerts_per_underlying_bar must be >= 1")
