from dataclasses import dataclass
from typing import List


@dataclass
class BacktestResult:
    symbol: str
    signal: str
    return_pct: float


class BacktestEngine:
    def run(self, signals: List[dict], benchmark_return: float = 0.05) -> List[BacktestResult]:
        results = []
        for signal in signals:
            outcome = benchmark_return if signal.get("action") == "buy" else -benchmark_return / 2
            results.append(
                BacktestResult(
                    symbol=signal.get("symbol", "UNKNOWN"),
                    signal=signal.get("action", "hold"),
                    return_pct=round(outcome, 4),
                )
            )
        return results
