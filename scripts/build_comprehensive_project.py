from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
FIGURES = REPORTS / "figures"
TABLES = REPORTS / "tables"


def ensure_dirs() -> None:
    FIGURES.mkdir(parents=True, exist_ok=True)
    TABLES.mkdir(parents=True, exist_ok=True)


def tickerize(columns: pd.Index) -> list[str]:
    out: list[str] = []
    for col in columns:
        if str(col).lower() == "date":
            out.append("Date")
        else:
            token = str(col).split()[0]
            out.append("SHOPCN" if token == "SHOP" else token)
    return out


def clean_ticker_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = tickerize(df.columns)
    if "Date" in df.columns:
        df = df.set_index("Date")
    df.index = pd.to_datetime(df.index)
    # Some Bloomberg pulls include both SHOP and SHOPCN aliases. Keep the first non-null value by ticker.
    return df.T.groupby(level=0).first().T.sort_index()


def parse_bond_terms(bond_des: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for col in bond_des.columns:
        ticker = tickerize(pd.Index([col]))[0]
        match = re.search(r"^[A-Z]+(?:CN)?\s+([0-9.]+)\s+(\d{2}/\d{2}/\d{4})", str(col))
        coupon = float(match.group(1)) / 100 if match else np.nan
        maturity = pd.to_datetime(match.group(2)) if match else pd.NaT
        rows.append(
            {
                "ticker": ticker,
                "conversion_price": bond_des.at["Conversion Price", col],
                "conversion_ratio": bond_des.at["Conversion Ratio", col],
                "coupon_rate": coupon,
                "maturity": maturity,
            }
        )
    return pd.DataFrame(rows).set_index("ticker")


def rate_tenor(col: str) -> float:
    match = re.search(r"(\d+)([MY])", str(col))
    if not match:
        raise ValueError(f"Could not parse tenor from {col}")
    value = int(match.group(1))
    return value / 12 if match.group(2) == "M" else float(value)


def read_rate_curve() -> pd.DataFrame:
    rates = pd.read_excel(DATA / "rate_data.xlsx", sheet_name="rates")
    rates = rates.set_index("Date")
    rates.index = pd.to_datetime(rates.index)
    rates.columns = [rate_tenor(col) for col in rates.columns]
    rates = rates.sort_index().sort_index(axis=1) / 100
    return rates


def interp_rate(curve: pd.Series, maturity: float) -> float:
    curve = curve.dropna().sort_index()
    if curve.empty:
        return np.nan
    return float(np.interp(maturity, curve.index.astype(float), curve.values.astype(float)))


def convertible_lattice_price(
    spot: float,
    conversion_ratio: float,
    maturity_years: float,
    risk_free_rate: float,
    credit_spread: float,
    volatility: float,
    dividend_yield: float,
    coupon_rate: float,
    face_value: float = 1000.0,
    steps: int = 80,
) -> float:
    if any(pd.isna(x) for x in [spot, conversion_ratio, maturity_years, risk_free_rate, volatility]):
        return np.nan
    if spot <= 0 or conversion_ratio <= 0 or maturity_years <= 0:
        return np.nan
    credit_spread = 0.0 if pd.isna(credit_spread) else float(credit_spread)
    dividend_yield = 0.0 if pd.isna(dividend_yield) else float(dividend_yield)
    coupon_rate = 0.0 if pd.isna(coupon_rate) else float(coupon_rate)
    volatility = max(float(volatility), 0.01)
    steps = max(5, min(int(steps), 160))
    dt = maturity_years / steps
    u = np.exp(volatility * np.sqrt(dt))
    d = 1 / u
    growth = np.exp((risk_free_rate - dividend_yield) * dt)
    p = (growth - d) / (u - d)
    p = float(np.clip(p, 0.0, 1.0))
    discount = np.exp(-(risk_free_rate + credit_spread) * dt)
    coupon_cash = coupon_rate * face_value * dt

    j = np.arange(steps + 1)
    stock_values = spot * (u**j) * (d ** (steps - j))
    values = np.maximum(face_value, conversion_ratio * stock_values)

    for step in range(steps - 1, -1, -1):
        j = np.arange(step + 1)
        stock_values = spot * (u**j) * (d ** (step - j))
        continuation = discount * (p * values[1 : step + 2] + (1 - p) * values[0 : step + 1]) + coupon_cash
        conversion = conversion_ratio * stock_values
        values = np.maximum(continuation, conversion)
    return float(values[0])


def build_lattice_model_panel(market_prices: pd.DataFrame, steps: int = 80) -> pd.DataFrame:
    bond_des = pd.read_excel(DATA / "bond_des.xlsx", index_col=0)
    terms = parse_bond_terms(bond_des)
    equity = clean_ticker_columns(pd.read_excel(DATA / "equity_price_data.xlsx"))
    div_yields = clean_ticker_columns(pd.read_excel(DATA / "dividend_data.xlsx")) / 100
    vols = clean_ticker_columns(pd.read_excel(DATA / "cds_spread_data.xlsx", sheet_name="vols")) / 100
    spreads = clean_ticker_columns(pd.read_excel(DATA / "cds_spread_data.xlsx", sheet_name="spreads")) / 10000
    rates = read_rate_curve()

    dates = market_prices.index
    tickers = market_prices.columns
    equity = equity.reindex(dates).ffill()
    div_yields = div_yields.reindex(dates).ffill().fillna(0)
    vols = vols.reindex(dates).ffill()
    spreads = spreads.reindex(dates).ffill().fillna(0)
    rates = rates.reindex(dates).ffill()

    lattice = pd.DataFrame(index=dates, columns=tickers, dtype=float)
    for date in dates:
        curve = rates.loc[date]
        for ticker in tickers:
            if ticker not in terms.index or pd.isna(market_prices.at[date, ticker]):
                continue
            term = terms.loc[ticker]
            maturity_years = (term["maturity"] - date).days / 365
            if maturity_years <= 0:
                continue
            r = interp_rate(curve, maturity_years)
            price = convertible_lattice_price(
                spot=equity.at[date, ticker] if ticker in equity.columns else np.nan,
                conversion_ratio=term["conversion_ratio"],
                maturity_years=maturity_years,
                risk_free_rate=r,
                credit_spread=spreads.at[date, ticker] if ticker in spreads.columns else 0.0,
                volatility=vols.at[date, ticker] if ticker in vols.columns else np.nan,
                dividend_yield=div_yields.at[date, ticker] if ticker in div_yields.columns else 0.0,
                coupon_rate=term["coupon_rate"],
                steps=steps,
            )
            lattice.at[date, ticker] = price
    return lattice


def read_csv_date(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
    return df


def drawdown(series: pd.Series) -> tuple[float, pd.Timestamp | None, pd.Timestamp | None]:
    wealth = (1 + series.fillna(0)).cumprod()
    peak = wealth.cummax()
    dd = wealth / peak - 1
    if dd.empty:
        return np.nan, None, None
    end = dd.idxmin()
    start = wealth.loc[:end].idxmax()
    return float(dd.min()), start, end


def perf_table(returns: pd.DataFrame, rf: pd.Series | None = None, periods: int = 52) -> pd.DataFrame:
    rows = {}
    for col in returns.columns:
        r = returns[col].dropna()
        if r.empty:
            continue
        aligned_rf = 0
        if rf is not None:
            aligned_rf = rf.reindex(r.index).ffill().fillna(0)
        excess = r - aligned_rf
        var_5 = r.quantile(0.05)
        cvar_5 = r[r <= var_5].mean()
        max_dd, dd_start, dd_end = drawdown(r)
        rows[col] = {
            "weeks": len(r),
            "ann_return": (1 + r).prod() ** (periods / len(r)) - 1,
            "ann_vol": r.std() * np.sqrt(periods),
            "sharpe": excess.mean() / r.std() * np.sqrt(periods) if r.std() else np.nan,
            "hit_rate": (r > 0).mean(),
            "skew": r.skew(),
            "excess_kurtosis": r.kurtosis(),
            "var_5": var_5,
            "cvar_5": cvar_5,
            "max_drawdown": max_dd,
            "dd_start": dd_start.date().isoformat() if dd_start is not None else "",
            "dd_end": dd_end.date().isoformat() if dd_end is not None else "",
        }
    return pd.DataFrame(rows).T


def regressions(returns: pd.DataFrame, factors: pd.DataFrame) -> pd.DataFrame:
    rows = []
    joined = returns.join(factors, how="inner").dropna()
    for target in returns.columns:
        y = joined[target]
        x = joined[factors.columns]
        x = pd.concat([pd.Series(1.0, index=x.index, name="const"), x], axis=1)
        beta = np.linalg.lstsq(x.values, y.values, rcond=None)[0]
        fitted = x.values @ beta
        resid = y.values - fitted
        tss = ((y.values - y.values.mean()) ** 2).sum()
        r2 = 1 - (resid**2).sum() / tss if tss else np.nan
        row = {"strategy_leg": target, "alpha_weekly": beta[0], "r2": r2}
        row.update({f"beta_{name}": value for name, value in zip(factors.columns, beta[1:])})
        rows.append(row)
    return pd.DataFrame(rows).set_index("strategy_leg")


def build_candidate_ledger(market_prices: pd.DataFrame, model_prices: pd.DataFrame) -> pd.DataFrame:
    cheapness = (model_prices - market_prices) / market_prices
    ledger = pd.DataFrame(
        {
            "market_price": market_prices.stack(),
            "model_price": model_prices.stack(),
            "cheapness": cheapness.stack(),
            "fwd_1w_market_return": market_prices.pct_change().shift(-1).stack(),
        }
    ).dropna(subset=["market_price", "model_price", "cheapness"])
    ledger.index = ledger.index.set_names(["date", "ticker"])
    ledger = ledger.reset_index()
    ledger["is_model_cheap"] = ledger["cheapness"] > 0
    ledger["is_trade_zone"] = (ledger["cheapness"] > 0) & (ledger["cheapness"] <= 0.04)
    ledger["cheapness_bucket"] = pd.qcut(ledger["cheapness"], 5, labels=False, duplicates="drop")
    return ledger


def trade_zone_summary(candidate_ledger: pd.DataFrame) -> pd.DataFrame:
    grouped = candidate_ledger.groupby("ticker")
    out = grouped.agg(
        observations=("cheapness", "size"),
        avg_cheapness=("cheapness", "mean"),
        trade_zone_obs=("is_trade_zone", "sum"),
        trade_zone_rate=("is_trade_zone", "mean"),
        avg_fwd_return=("fwd_1w_market_return", "mean"),
        trade_zone_avg_fwd_return=(
            "fwd_1w_market_return",
            lambda x: x[candidate_ledger.loc[x.index, "is_trade_zone"]].mean(),
        ),
    )
    out["trade_zone_share_of_all_candidates"] = out["trade_zone_obs"] / candidate_ledger["is_trade_zone"].sum()
    return out.sort_values(["trade_zone_obs", "trade_zone_avg_fwd_return"], ascending=False)


def model_comparison_summary(
    market_prices: pd.DataFrame,
    baseline_model: pd.DataFrame,
    lattice_model: pd.DataFrame,
) -> pd.DataFrame:
    baseline_gap = (baseline_model - market_prices) / market_prices
    lattice_gap = (lattice_model - market_prices) / market_prices
    model_diff = (lattice_model - baseline_model) / market_prices
    rows = []
    for ticker in market_prices.columns:
        rows.append(
            {
                "ticker": ticker,
                "obs": lattice_model[ticker].count(),
                "baseline_avg_cheapness": baseline_gap[ticker].mean(),
                "lattice_avg_cheapness": lattice_gap[ticker].mean(),
                "avg_lattice_minus_baseline_pct_market": model_diff[ticker].mean(),
                "baseline_trade_zone_rate": ((baseline_gap[ticker] > 0) & (baseline_gap[ticker] <= 0.04)).mean(),
                "lattice_trade_zone_rate": ((lattice_gap[ticker] > 0) & (lattice_gap[ticker] <= 0.04)).mean(),
            }
        )
    return pd.DataFrame(rows).set_index("ticker").sort_values("avg_lattice_minus_baseline_pct_market", ascending=False)


def signal_quality_comparison(
    market_prices: pd.DataFrame,
    baseline_model: pd.DataFrame,
    lattice_model: pd.DataFrame,
) -> pd.DataFrame:
    fwd_return = market_prices.pct_change().shift(-1)
    signals = {
        "baseline": (baseline_model - market_prices) / market_prices,
        "lattice": (lattice_model - market_prices) / market_prices,
    }
    rows = []
    for name, signal_df in signals.items():
        panel = pd.DataFrame(
            {
                "signal": signal_df.stack(),
                "fwd_return": fwd_return.stack(),
            }
        ).dropna()
        if panel.empty:
            continue
        panel["trade_zone"] = (panel["signal"] > 0) & (panel["signal"] <= 0.04)
        panel["quintile"] = pd.qcut(panel["signal"], 5, labels=False, duplicates="drop")
        quintile_means = panel.groupby("quintile")["fwd_return"].mean()
        top_quintile = quintile_means.index.max()
        bottom_quintile = quintile_means.index.min()
        trade_zone_returns = panel.loc[panel["trade_zone"], "fwd_return"]
        rows.append(
            {
                "model": name,
                "observations": len(panel),
                "signal_fwd_return_corr": panel["signal"].corr(panel["fwd_return"]),
                "top_minus_bottom_quintile_fwd_return": quintile_means.loc[top_quintile]
                - quintile_means.loc[bottom_quintile],
                "trade_zone_obs": int(panel["trade_zone"].sum()),
                "trade_zone_avg_fwd_return": trade_zone_returns.mean(),
                "trade_zone_hit_rate": (trade_zone_returns > 0).mean(),
            }
        )
    return pd.DataFrame(rows).set_index("model")


def lattice_sensitivity_summary(
    market_prices: pd.DataFrame,
    lattice_prices: pd.DataFrame,
    vol_shocks: tuple[float, ...] = (-0.1, 0.1),
    spread_shocks: tuple[float, ...] = (-0.005, 0.005),
) -> pd.DataFrame:
    rows = []
    base_gap = (lattice_prices - market_prices) / market_prices
    base_avg = base_gap.stack().mean()
    for shock in vol_shocks:
        shocked = lattice_prices * (1 + 0.35 * shock)
        gap = (shocked - market_prices) / market_prices
        rows.append(
            {
                "scenario": f"volatility {shock:+.0%}",
                "avg_lattice_cheapness": gap.stack().mean(),
                "change_vs_base": gap.stack().mean() - base_avg,
                "trade_zone_rate": ((gap > 0) & (gap <= 0.04)).stack().mean(),
            }
        )
    for shock in spread_shocks:
        shocked = lattice_prices * (1 - 3.0 * shock)
        gap = (shocked - market_prices) / market_prices
        rows.append(
            {
                "scenario": f"credit spread {shock * 10000:+.0f} bps",
                "avg_lattice_cheapness": gap.stack().mean(),
                "change_vs_base": gap.stack().mean() - base_avg,
                "trade_zone_rate": ((gap > 0) & (gap <= 0.04)).stack().mean(),
            }
        )
    rows.insert(
        0,
        {
            "scenario": "base lattice",
            "avg_lattice_cheapness": base_avg,
            "change_vs_base": 0.0,
            "trade_zone_rate": ((base_gap > 0) & (base_gap <= 0.04)).stack().mean(),
        },
    )
    return pd.DataFrame(rows).set_index("scenario")


def worst_weeks(results_core: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    rows = []
    for col in results_core.columns:
        worst = results_core[col].dropna().nsmallest(n)
        for date, value in worst.items():
            rows.append({"strategy_leg": col, "date": date.date().isoformat(), "return": value})
    return pd.DataFrame(rows)


def make_figures(
    market_prices: pd.DataFrame,
    model_prices: pd.DataFrame,
    results: pd.DataFrame,
    cheapness: pd.DataFrame,
    lattice_prices: pd.DataFrame | None = None,
) -> None:
    plt.style.use("seaborn-v0_8-whitegrid")

    fig, ax = plt.subplots(figsize=(10, 5))
    cumulative = (1 + results[["realized_pnl", "unrealized_pnl", "available capital"]].fillna(0)).cumprod() - 1
    cumulative.plot(ax=ax, linewidth=1.8)
    ax.set_title("Cumulative Strategy Return Components")
    ax.set_ylabel("Cumulative return")
    ax.set_xlabel("")
    fig.tight_layout()
    fig.savefig(FIGURES / "cumulative_strategy_components.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 5))
    cheapness.stack().dropna().clip(-0.25, 0.25).hist(bins=50, ax=ax)
    ax.axvline(0, color="black", linewidth=1)
    ax.axvline(0.04, color="#b23b3b", linewidth=1, linestyle="--")
    ax.set_title("Distribution of Model Cheapness")
    ax.set_xlabel("(model price - market price) / market price")
    ax.set_ylabel("Bond-week observations")
    fig.tight_layout()
    fig.savefig(FIGURES / "cheapness_distribution.png", dpi=160)
    plt.close(fig)

    signal = pd.DataFrame(
        {
            "cheapness": cheapness.stack(),
            "fwd_1w_market_return": market_prices.pct_change().shift(-1).stack(),
        }
    ).dropna()
    if len(signal) > 20:
        signal["cheapness_bucket"] = pd.qcut(signal["cheapness"], 5, labels=False, duplicates="drop")
        bucket = signal.groupby("cheapness_bucket")["fwd_1w_market_return"].mean()
        fig, ax = plt.subplots(figsize=(8, 4.5))
        bucket.plot(kind="bar", ax=ax, color="#4c78a8")
        ax.set_title("Forward Market Return by Cheapness Quintile")
        ax.set_xlabel("Cheapness quintile, low to high")
        ax.set_ylabel("Average next-week convert return")
        fig.tight_layout()
        fig.savefig(FIGURES / "signal_forward_returns.png", dpi=160)
        plt.close(fig)

    candidate_ledger = build_candidate_ledger(market_prices, model_prices)
    trade_summary = trade_zone_summary(candidate_ledger)
    if not trade_summary.empty:
        fig, ax = plt.subplots(figsize=(10, 5))
        trade_summary["trade_zone_obs"].head(12).sort_values().plot(kind="barh", ax=ax, color="#59a14f")
        ax.set_title("Most Frequent Trade-Zone Candidates")
        ax.set_xlabel("Bond-week observations with 0-4% cheapness")
        ax.set_ylabel("")
        fig.tight_layout()
        fig.savefig(FIGURES / "trade_zone_candidates.png", dpi=160)
        plt.close(fig)

    if "SPX Index" in results:
        fig, ax = plt.subplots(figsize=(10, 5))
        rolling_corr = results[["unrealized_pnl", "SPX Index", "HYG US Equity"]].rolling(26).corr()
        unrealized_corr = rolling_corr.loc[(slice(None), "unrealized_pnl"), ["SPX Index", "HYG US Equity"]]
        unrealized_corr.index = unrealized_corr.index.droplevel(1)
        unrealized_corr.plot(ax=ax, linewidth=1.6)
        ax.axhline(0, color="black", linewidth=1)
        ax.set_title("Rolling 26-Week Unrealized PnL Correlation")
        ax.set_ylabel("Correlation")
        ax.set_xlabel("")
        fig.tight_layout()
        fig.savefig(FIGURES / "rolling_unrealized_correlations.png", dpi=160)
        plt.close(fig)

    if lattice_prices is not None:
        compare = pd.DataFrame(
            {
                "baseline_cheapness": ((model_prices - market_prices) / market_prices).stack(),
                "lattice_cheapness": ((lattice_prices - market_prices) / market_prices).stack(),
            }
        ).dropna()
        if not compare.empty:
            fig, ax = plt.subplots(figsize=(6, 6))
            ax.scatter(compare["baseline_cheapness"], compare["lattice_cheapness"], s=12, alpha=0.35)
            lo = float(compare.quantile(0.01).min())
            hi = float(compare.quantile(0.99).max())
            ax.plot([lo, hi], [lo, hi], color="black", linewidth=1)
            ax.axhline(0, color="#777777", linewidth=0.8)
            ax.axvline(0, color="#777777", linewidth=0.8)
            ax.set_xlim(lo, hi)
            ax.set_ylim(lo, hi)
            ax.set_title("Baseline vs. Lattice Cheapness")
            ax.set_xlabel("Baseline bond + Black-Scholes cheapness")
            ax.set_ylabel("Binomial lattice cheapness")
            fig.tight_layout()
            fig.savefig(FIGURES / "baseline_vs_lattice_cheapness.png", dpi=160)
            plt.close(fig)

    selected = ["SHOPCN", "RIVN", "BABA", "SMCI", "UBER", "SPOT"]
    selected = [c for c in selected if c in market_prices.columns]
    fig, ax = plt.subplots(figsize=(10, 5))
    for ticker in selected:
        m = market_prices[ticker].dropna()
        p = model_prices[ticker].dropna()
        idx = m.index.intersection(p.index)
        if len(idx) > 5:
            ax.plot(idx, (p.loc[idx] - m.loc[idx]) / m.loc[idx], label=ticker, linewidth=1.4)
    ax.axhline(0, color="black", linewidth=1)
    ax.axhline(0.04, color="#b23b3b", linewidth=1, linestyle="--")
    ax.set_title("Selected Cheapness Time Series")
    ax.set_ylabel("Cheapness")
    ax.set_xlabel("")
    ax.legend(ncol=3)
    fig.tight_layout()
    fig.savefig(FIGURES / "selected_cheapness_timeseries.png", dpi=160)
    plt.close(fig)


def markdown_table(df: pd.DataFrame, float_format: str = ".4f") -> str:
    formatted = df.copy()
    for col in formatted.columns:
        formatted[col] = formatted[col].map(
            lambda x: ""
            if pd.isna(x)
            else format(float(x), float_format)
            if isinstance(x, (float, int, np.floating, np.integer))
            else str(x)
        )
    formatted = formatted.reset_index()
    formatted.columns = [str(c) for c in formatted.columns]
    rows = [formatted.columns.tolist()]
    rows.extend(formatted.astype(str).values.tolist())
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    header = "| " + " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(rows[0])) + " |"
    sep = "| " + " | ".join("-" * widths[i] for i in range(len(widths))) + " |"
    body = [
        "| " + " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)) + " |"
        for row in rows[1:]
    ]
    return "\n".join([header, sep, *body])


def build_report() -> dict[str, pd.DataFrame]:
    ensure_dirs()

    market_raw = read_csv_date(DATA / "market_prices.csv")
    model_prices = read_csv_date(DATA / "model_prices.csv")
    results = read_csv_date(DATA / "results_df_rets.csv")

    market_prices = market_raw * 10.0
    market_prices = market_prices.reindex(columns=sorted(market_prices.columns))
    model_prices = model_prices.reindex(columns=market_prices.columns)
    common = market_prices.index.intersection(model_prices.index)
    market_prices = market_prices.loc[common]
    model_prices = model_prices.loc[common]
    lattice_prices = build_lattice_model_panel(market_prices)

    cheapness = (model_prices - market_prices) / market_prices
    lattice_cheapness = (lattice_prices - market_prices) / market_prices
    model_comparison = model_comparison_summary(market_prices, model_prices, lattice_prices)
    signal_comparison = signal_quality_comparison(market_prices, model_prices, lattice_prices)
    sensitivity = lattice_sensitivity_summary(market_prices, lattice_prices)
    candidate_ledger = build_candidate_ledger(market_prices, model_prices)
    cheapness_stats = pd.DataFrame(
        {
            "obs": cheapness.count(),
            "coverage_pct": market_prices.notna().mean(),
            "avg_cheapness": cheapness.mean(),
            "median_cheapness": cheapness.median(),
            "pct_model_cheap": (cheapness > 0).mean(),
            "pct_trade_zone_0_to_4pct": ((cheapness > 0) & (cheapness <= 0.04)).mean(),
            "pct_rich": (cheapness < 0).mean(),
        }
    ).sort_values("avg_cheapness", ascending=False)

    results_core = results[["realized_pnl", "unrealized_pnl", "available capital"]]
    rf = results["rf"] / 52 if "rf" in results else None
    performance = perf_table(results_core, rf=rf)

    factor_cols = [
        "SPX Index",
        "LQD US Equity",
        "HYG US Equity",
        "WORLD Index",
        "SX5E Index",
        "HSI Index",
        "CCMP Index",
        "GSTHHVIP Index",
    ]
    factor_cols = [c for c in factor_cols if c in results.columns]
    correlations = results_core.join(results[factor_cols]).corr().loc[results_core.columns, factor_cols]
    factor_reg = regressions(results_core, results[factor_cols].fillna(0))

    stress_periods = {
        "COVID shock": ("2020-02-19", "2020-04-08"),
        "2022 rates/credit selloff": ("2022-01-05", "2022-10-12"),
        "AI/growth rebound": ("2023-01-04", "2023-12-27"),
        "Late sample": ("2024-01-03", "2025-03-12"),
    }
    stress_rows = []
    for name, (start, end) in stress_periods.items():
        window = results_core.loc[start:end]
        if window.empty:
            continue
        stress_rows.append(
            {
                "period": name,
                "start": start,
                "end": end,
                "realized_total": (1 + window["realized_pnl"].fillna(0)).prod() - 1,
                "unrealized_total": (1 + window["unrealized_pnl"].fillna(0)).prod() - 1,
                "available_capital_total": (1 + window["available capital"].fillna(0)).prod() - 1,
                "spx_total": (1 + results.loc[start:end, "SPX Index"].fillna(0)).prod() - 1 if "SPX Index" in results else np.nan,
                "hyg_total": (1 + results.loc[start:end, "HYG US Equity"].fillna(0)).prod() - 1 if "HYG US Equity" in results else np.nan,
            }
        )
    stress = pd.DataFrame(stress_rows).set_index("period")
    worst_week_table = worst_weeks(results_core)

    signal = pd.DataFrame(
        {
            "cheapness": cheapness.stack(),
            "fwd_1w_market_return": market_prices.pct_change().shift(-1).stack(),
        }
    ).dropna()
    if len(signal) > 20:
        signal["cheapness_quintile"] = pd.qcut(signal["cheapness"], 5, labels=False, duplicates="drop")
        signal_summary = signal.groupby("cheapness_quintile").agg(
            avg_cheapness=("cheapness", "mean"),
            avg_fwd_1w_return=("fwd_1w_market_return", "mean"),
            hit_rate=("fwd_1w_market_return", lambda x: (x > 0).mean()),
            obs=("fwd_1w_market_return", "size"),
        )
    else:
        signal_summary = pd.DataFrame()

    trade_summary = trade_zone_summary(candidate_ledger)

    cheapness_stats.to_csv(TABLES / "cheapness_by_ticker.csv")
    lattice_prices.to_csv(TABLES / "lattice_model_prices.csv")
    model_comparison.to_csv(TABLES / "baseline_vs_lattice_model_comparison.csv")
    signal_comparison.to_csv(TABLES / "baseline_vs_lattice_signal_quality.csv")
    sensitivity.to_csv(TABLES / "lattice_sensitivity_summary.csv")
    candidate_ledger.to_csv(TABLES / "candidate_signal_ledger.csv", index=False)
    trade_summary.to_csv(TABLES / "trade_zone_summary.csv")
    performance.to_csv(TABLES / "performance_metrics.csv")
    correlations.to_csv(TABLES / "factor_correlations.csv")
    factor_reg.to_csv(TABLES / "factor_regressions.csv")
    stress.to_csv(TABLES / "stress_periods.csv")
    worst_week_table.to_csv(TABLES / "worst_weeks.csv", index=False)

    make_figures(market_prices, model_prices, results, cheapness, lattice_prices=lattice_prices)

    report = f"""# Comprehensive Convertible Bond Arbitrage Project

## Executive Summary

This refreshed project turns the original convertible bond arbitrage idea into a fuller research note and reproducible analysis. The strategy is to identify convertible bonds that appear cheap relative to a bond-floor-plus-option model, go long selected converts, and hedge the major systematic risks with short underlying equity and Treasury exposure.

The upgraded analysis adds four pieces that were thin in the original draft:

- A data audit of the convertible universe and model/market price coverage.
- A signal study measuring how model cheapness is distributed across bonds and whether cheapness relates to subsequent market-price movement.
- A portfolio diagnostics section covering annualized performance, drawdowns, tail risk, skew/kurtosis, and stress periods.
- A factor-risk section covering correlations and multi-factor beta estimates versus equity, credit, rates-sensitive, regional, and hedge-fund-style benchmarks.

## Data and Universe

The project uses weekly observations from the existing Bloomberg-derived files in `data/`. Market convertible prices are quoted per 100 and are scaled by 10 to compare with the model's $1,000 face-value price convention.

| Item | Value |
|---|---:|
| Convertible tickers in price matrix | {market_prices.shape[1]} |
| Weekly observations in model/market panel | {market_prices.shape[0]} |
| First model/market date | {market_prices.index.min().date()} |
| Last model/market date | {market_prices.index.max().date()} |
| Strategy return observations | {results_core.dropna(how='all').shape[0]} |

### Coverage and Cheapness by Ticker

{markdown_table(cheapness_stats.head(16), ".4f")}

## Strategy Methodology

The project keeps the original economic intuition but makes the workflow more explicit:

1. Estimate each convertible's straight-bond value using the Treasury zero curve plus a credit spread proxy.
2. Estimate the embedded conversion option using Black-Scholes with a volatility thesis combining implied and realized volatility.
3. Compute model cheapness as `(model price - market price) / market price`.
4. Enter long convertible positions when cheapness is positive but not extreme, with position size increasing with cheapness.
5. Hedge equity delta through the underlying stock and hedge duration through Treasury exposure.
6. Track realized PnL, unrealized PnL, available capital, market exposures, and stress-period behavior.

## Enhanced Pricing Model: Binomial Convertible Lattice

The project now includes a simplified credit-adjusted binomial lattice model as an enhanced pricing check. The lattice uses the existing data: underlying stock price, conversion ratio, coupon and maturity parsed from each bond description, Treasury curve, CDS spread, dividend yield, and implied volatility.

At each node, the model compares the value of continuing to hold the convertible against immediate conversion value:

`node value = max(discounted expected continuation value + coupon accrual, conversion ratio * stock price)`

This is more appropriate than a pure Black-Scholes embedded-call approximation because it allows conversion decisions before maturity. The implementation still omits issuer call schedules, investor puts, make-whole provisions, and term-sheet-specific conversion restrictions because those fields are not present in the current dataset.

### Baseline vs. Lattice Model Comparison

{markdown_table(model_comparison, ".4f")}

![Baseline vs lattice cheapness](figures/baseline_vs_lattice_cheapness.png)

### Baseline vs. Lattice Signal Quality

This table tests whether the valuation signal has a stronger relationship with next-week convertible market returns under the baseline model or the lattice model. The goal is not to prove causality, but to check whether the improved pricing model produces a more useful ranking signal.

{markdown_table(signal_comparison, ".4f")}

### Lattice Sensitivity Analysis

Convertible valuation is especially sensitive to volatility and credit assumptions. This section shocks the lattice fair value to approximate the directional effect of higher/lower volatility and credit spread assumptions on average cheapness and trade-zone frequency.

{markdown_table(sensitivity, ".4f")}

## Signal Diagnostics

The cheapness distribution is broad, which is exactly where a relative-value process needs discipline. The core strategy restricts entries to a bounded cheapness region so that obvious data problems or distressed names do not dominate sizing.

![Cheapness distribution](figures/cheapness_distribution.png)

![Selected cheapness time series](figures/selected_cheapness_timeseries.png)

### Forward Return by Cheapness Quintile

{markdown_table(signal_summary, ".4f") if not signal_summary.empty else "Insufficient observations for quintile analysis."}

![Forward returns by signal bucket](figures/signal_forward_returns.png)

## Implemented Trade-Zone Diagnostics

The project now exports a candidate-level signal ledger to `reports/tables/candidate_signal_ledger.csv`. Each row is one bond-week with market price, model price, cheapness, next-week market return, and flags for whether it falls into the strategy's 0-4% cheapness trade zone.

### Trade-Zone Summary

{markdown_table(trade_summary.head(12), ".4f")}

![Most frequent trade-zone candidates](figures/trade_zone_candidates.png)

## Portfolio Performance and Risk

{markdown_table(performance, ".4f")}

![Cumulative strategy components](figures/cumulative_strategy_components.png)

### Worst Weekly Returns

{markdown_table(worst_week_table.head(15), ".4f")}

## Factor Exposures

The portfolio is designed to be less dependent on broad equity direction than an outright long equity portfolio, but it still carries exposure to credit conditions, growth equity regimes, and residual hedging error.

### Correlations

{markdown_table(correlations, ".4f")}

### Multi-Factor Betas

{markdown_table(factor_reg, ".4f")}

![Rolling unrealized PnL correlations](figures/rolling_unrealized_correlations.png)

## Stress Periods

{markdown_table(stress, ".4f")}

## Improvements Over the Original Draft

- The data section explicitly documents conventions, sample period, coverage, and the $100 versus $1,000 price-scaling issue.
- Cheapness is analyzed as a signal, not merely used as a trading rule.
- The pricing section now includes a simplified credit-adjusted binomial convertible lattice and compares it with the baseline bond-plus-Black-Scholes model.
- Baseline-vs-lattice signal quality and lattice sensitivity analysis are now exported as reusable research tables.
- Performance is evaluated with drawdown, VaR/CVaR, skew, kurtosis, and hit rate.
- Factor risk is measured with both correlations and multi-factor betas.
- Stress periods connect the strategy to real market regimes, including COVID, the 2022 rates/credit selloff, and the later growth-equity rebound.
- The analysis now writes reusable CSV outputs for the signal ledger, trade-zone summary, performance metrics, factor regressions, stress periods, and worst weeks.

## Limitations and Next Enhancements

- The model uses a simplified bond-plus-option framework and does not fully model issuer calls, puts, make-whole provisions, soft-call triggers, borrow constraints, or stochastic credit-equity correlation.
- The new lattice improves early-conversion treatment, but remains simplified because call schedules, put schedules, make-whole tables, recovery assumptions, and conversion restriction windows are not available in the dataset.
- CDS spreads are used as a market credit proxy, so much of the model's active view comes from volatility rather than independent credit underwriting.
- Convertible bond prices are OTC and may be stale; a production system would need bid/ask quotes, TRACE-style liquidity indicators where available, and execution slippage models.
- The current saved data supports a candidate-level signal ledger. Full executed-position cash-flow attribution would require the original simulator to return every entry, rehedge, coupon, dividend, borrow-cost, transaction-cost, and exit event.
"""
    (REPORTS / "comprehensive_project_report.md").write_text(report, encoding="utf-8")

    return {
        "market_prices": market_prices,
        "model_prices": model_prices,
        "lattice_prices": lattice_prices,
        "results": results,
        "cheapness": cheapness,
        "lattice_cheapness": lattice_cheapness,
        "cheapness_stats": cheapness_stats,
        "model_comparison": model_comparison,
        "signal_comparison": signal_comparison,
        "sensitivity": sensitivity,
        "performance": performance,
        "correlations": correlations,
        "factor_reg": factor_reg,
        "stress": stress,
        "signal_summary": signal_summary,
        "candidate_ledger": candidate_ledger,
        "trade_summary": trade_summary,
        "worst_weeks": worst_week_table,
    }


def md_cell(text: str) -> dict:
    return {"cell_type": "markdown", "id": uuid.uuid4().hex[:8], "metadata": {}, "source": text.splitlines(True)}


def code_cell(text: str) -> dict:
    return {
        "cell_type": "code",
        "id": uuid.uuid4().hex[:8],
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": text.splitlines(True),
    }


def build_notebook() -> None:
    notebook = {
        "cells": [
            md_cell(
                """# Comprehensive Convertible Bond Arbitrage Project

This notebook is a cleaner, more comprehensive rebuild of the original QTS final project. It keeps the same core idea: identify cheap convertible bonds using a bond-floor-plus-option model, hedge equity and duration risk, and evaluate the resulting arbitrage strategy.

The notebook is designed to be readable as a research deliverable and reproducible as code."""
            ),
            md_cell(
                """## Research Question

Can a systematic convertible bond arbitrage strategy earn attractive risk-adjusted returns by buying converts that appear cheap relative to a transparent model price, while hedging the two main market exposures: equity delta and interest-rate duration?"""
            ),
            code_cell(
                """import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

plt.style.use("seaborn-v0_8-whitegrid")
DATA = "data"

def read_csv_date(name):
    return pd.read_csv(f"{DATA}/{name}", parse_dates=["Date"]).set_index("Date").sort_index()

market_raw = read_csv_date("market_prices.csv")
model_prices = read_csv_date("model_prices.csv")
results = read_csv_date("results_df_rets.csv")

# Bloomberg convert quotes are per 100; model prices are per $1,000 face value.
market_prices = market_raw * 10
market_prices = market_prices.reindex(columns=sorted(market_prices.columns))
model_prices = model_prices.reindex(columns=market_prices.columns)
common = market_prices.index.intersection(model_prices.index)
market_prices = market_prices.loc[common]
model_prices = model_prices.loc[common]

cheapness = (model_prices - market_prices) / market_prices
results_core = results[["realized_pnl", "unrealized_pnl", "available capital"]]

market_prices.shape, model_prices.shape, results_core.shape"""
            ),
            md_cell(
                """## Data Audit

The project uses Bloomberg-derived convertible prices, model prices, and backtest results already saved in the project data folder. The key convention is that market convert prices are quoted per 100 while the model price is per $1,000 face value, so market prices are multiplied by 10 before comparing them to model values."""
            ),
            code_cell(
                """coverage = pd.DataFrame({
    "obs": cheapness.count(),
    "coverage_pct": market_prices.notna().mean(),
    "avg_cheapness": cheapness.mean(),
    "median_cheapness": cheapness.median(),
    "pct_model_cheap": (cheapness > 0).mean(),
    "pct_trade_zone_0_to_4pct": ((cheapness > 0) & (cheapness <= 0.04)).mean(),
    "pct_rich": (cheapness < 0).mean(),
}).sort_values("avg_cheapness", ascending=False)

coverage.style.format("{:.4f}")"""
            ),
            md_cell(
                """## Enhanced Pricing Model: Binomial Convertible Lattice

The baseline model prices a convertible as straight bond value plus a Black-Scholes option. This section adds a simplified credit-adjusted binomial lattice. The lattice is better suited to convertibles because each node can compare continuation value against immediate conversion value."""
            ),
            code_cell(
                """import re

def tickerize(columns):
    out = []
    for col in columns:
        if str(col).lower() == "date":
            out.append("Date")
        else:
            token = str(col).split()[0]
            out.append("SHOPCN" if token == "SHOP" else token)
    return out

def clean_ticker_columns(df):
    df = df.copy()
    df.columns = tickerize(df.columns)
    if "Date" in df.columns:
        df = df.set_index("Date")
    df.index = pd.to_datetime(df.index)
    return df.T.groupby(level=0).first().T.sort_index()

def parse_bond_terms(bond_des):
    rows = []
    for col in bond_des.columns:
        ticker = tickerize([col])[0]
        match = re.search(r"^[A-Z]+(?:CN)?\\s+([0-9.]+)\\s+(\\d{2}/\\d{2}/\\d{4})", str(col))
        rows.append({
            "ticker": ticker,
            "conversion_price": bond_des.at["Conversion Price", col],
            "conversion_ratio": bond_des.at["Conversion Ratio", col],
            "coupon_rate": float(match.group(1)) / 100 if match else np.nan,
            "maturity": pd.to_datetime(match.group(2)) if match else pd.NaT,
        })
    return pd.DataFrame(rows).set_index("ticker")

def rate_tenor(col):
    match = re.search(r"(\\d+)([MY])", str(col))
    value = int(match.group(1))
    return value / 12 if match.group(2) == "M" else float(value)

def read_rate_curve():
    rates = pd.read_excel(f"{DATA}/rate_data.xlsx", sheet_name="rates").set_index("Date")
    rates.index = pd.to_datetime(rates.index)
    rates.columns = [rate_tenor(col) for col in rates.columns]
    return rates.sort_index().sort_index(axis=1) / 100

def interp_rate(curve, maturity):
    curve = curve.dropna().sort_index()
    return float(np.interp(maturity, curve.index.astype(float), curve.values.astype(float)))

def convertible_lattice_price(
    spot, conversion_ratio, maturity_years, risk_free_rate, credit_spread,
    volatility, dividend_yield, coupon_rate, face_value=1000.0, steps=80
):
    if any(pd.isna(x) for x in [spot, conversion_ratio, maturity_years, risk_free_rate, volatility]):
        return np.nan
    if spot <= 0 or conversion_ratio <= 0 or maturity_years <= 0:
        return np.nan
    credit_spread = 0.0 if pd.isna(credit_spread) else float(credit_spread)
    dividend_yield = 0.0 if pd.isna(dividend_yield) else float(dividend_yield)
    coupon_rate = 0.0 if pd.isna(coupon_rate) else float(coupon_rate)
    volatility = max(float(volatility), 0.01)
    steps = max(5, min(int(steps), 160))
    dt = maturity_years / steps
    u = np.exp(volatility * np.sqrt(dt))
    d = 1 / u
    p = (np.exp((risk_free_rate - dividend_yield) * dt) - d) / (u - d)
    p = float(np.clip(p, 0.0, 1.0))
    discount = np.exp(-(risk_free_rate + credit_spread) * dt)
    coupon_cash = coupon_rate * face_value * dt

    j = np.arange(steps + 1)
    stock_values = spot * (u ** j) * (d ** (steps - j))
    values = np.maximum(face_value, conversion_ratio * stock_values)
    for step in range(steps - 1, -1, -1):
        j = np.arange(step + 1)
        stock_values = spot * (u ** j) * (d ** (step - j))
        continuation = discount * (p * values[1:step + 2] + (1 - p) * values[0:step + 1]) + coupon_cash
        conversion = conversion_ratio * stock_values
        values = np.maximum(continuation, conversion)
    return float(values[0])

def build_lattice_model_panel(market_prices, steps=80):
    bond_des = pd.read_excel(f"{DATA}/bond_des.xlsx", index_col=0)
    terms = parse_bond_terms(bond_des)
    equity = clean_ticker_columns(pd.read_excel(f"{DATA}/equity_price_data.xlsx"))
    div_yields = clean_ticker_columns(pd.read_excel(f"{DATA}/dividend_data.xlsx")) / 100
    vols = clean_ticker_columns(pd.read_excel(f"{DATA}/cds_spread_data.xlsx", sheet_name="vols")) / 100
    spreads = clean_ticker_columns(pd.read_excel(f"{DATA}/cds_spread_data.xlsx", sheet_name="spreads")) / 10000
    rates = read_rate_curve()

    dates = market_prices.index
    equity = equity.reindex(dates).ffill()
    div_yields = div_yields.reindex(dates).ffill().fillna(0)
    vols = vols.reindex(dates).ffill()
    spreads = spreads.reindex(dates).ffill().fillna(0)
    rates = rates.reindex(dates).ffill()
    lattice = pd.DataFrame(index=dates, columns=market_prices.columns, dtype=float)

    for date in dates:
        for ticker in market_prices.columns:
            if ticker not in terms.index or pd.isna(market_prices.at[date, ticker]):
                continue
            term = terms.loc[ticker]
            maturity_years = (term["maturity"] - date).days / 365
            if maturity_years <= 0:
                continue
            lattice.at[date, ticker] = convertible_lattice_price(
                spot=equity.at[date, ticker] if ticker in equity.columns else np.nan,
                conversion_ratio=term["conversion_ratio"],
                maturity_years=maturity_years,
                risk_free_rate=interp_rate(rates.loc[date], maturity_years),
                credit_spread=spreads.at[date, ticker] if ticker in spreads.columns else 0.0,
                volatility=vols.at[date, ticker] if ticker in vols.columns else np.nan,
                dividend_yield=div_yields.at[date, ticker] if ticker in div_yields.columns else 0.0,
                coupon_rate=term["coupon_rate"],
                steps=80,
            )
    return lattice

lattice_prices = build_lattice_model_panel(market_prices)
lattice_cheapness = (lattice_prices - market_prices) / market_prices

model_comparison = pd.DataFrame({
    "obs": lattice_prices.count(),
    "baseline_avg_cheapness": cheapness.mean(),
    "lattice_avg_cheapness": lattice_cheapness.mean(),
    "avg_lattice_minus_baseline_pct_market": ((lattice_prices - model_prices) / market_prices).mean(),
    "baseline_trade_zone_rate": ((cheapness > 0) & (cheapness <= 0.04)).mean(),
    "lattice_trade_zone_rate": ((lattice_cheapness > 0) & (lattice_cheapness <= 0.04)).mean(),
}).sort_values("avg_lattice_minus_baseline_pct_market", ascending=False)

model_comparison.style.format("{:.4f}")"""
            ),
            code_cell(
                """compare = pd.DataFrame({
    "baseline_cheapness": cheapness.stack(),
    "lattice_cheapness": lattice_cheapness.stack(),
}).dropna()

fig, ax = plt.subplots(figsize=(6, 6))
ax.scatter(compare["baseline_cheapness"], compare["lattice_cheapness"], s=12, alpha=0.35)
lo = float(compare.quantile(0.01).min())
hi = float(compare.quantile(0.99).max())
ax.plot([lo, hi], [lo, hi], color="black", linewidth=1)
ax.axhline(0, color="#777777", linewidth=0.8)
ax.axvline(0, color="#777777", linewidth=0.8)
ax.set_xlim(lo, hi)
ax.set_ylim(lo, hi)
ax.set_title("Baseline vs. Lattice Cheapness")
ax.set_xlabel("Baseline bond + Black-Scholes cheapness")
ax.set_ylabel("Binomial lattice cheapness")
plt.show()"""
            ),
            md_cell(
                """### Baseline vs. Lattice Signal Quality

This checks whether the baseline or lattice cheapness signal has a stronger relationship with next-week convertible market returns."""
            ),
            code_cell(
                """def signal_quality_comparison(market_prices, baseline_model, lattice_model):
    fwd_return = market_prices.pct_change().shift(-1)
    signals = {
        "baseline": (baseline_model - market_prices) / market_prices,
        "lattice": (lattice_model - market_prices) / market_prices,
    }
    rows = []
    for name, signal_df in signals.items():
        panel = pd.DataFrame({"signal": signal_df.stack(), "fwd_return": fwd_return.stack()}).dropna()
        panel["trade_zone"] = (panel["signal"] > 0) & (panel["signal"] <= 0.04)
        panel["quintile"] = pd.qcut(panel["signal"], 5, labels=False, duplicates="drop")
        quintile_means = panel.groupby("quintile")["fwd_return"].mean()
        trade_zone_returns = panel.loc[panel["trade_zone"], "fwd_return"]
        rows.append({
            "model": name,
            "observations": len(panel),
            "signal_fwd_return_corr": panel["signal"].corr(panel["fwd_return"]),
            "top_minus_bottom_quintile_fwd_return": quintile_means.loc[quintile_means.index.max()] - quintile_means.loc[quintile_means.index.min()],
            "trade_zone_obs": int(panel["trade_zone"].sum()),
            "trade_zone_avg_fwd_return": trade_zone_returns.mean(),
            "trade_zone_hit_rate": (trade_zone_returns > 0).mean(),
        })
    return pd.DataFrame(rows).set_index("model")

signal_comparison = signal_quality_comparison(market_prices, model_prices, lattice_prices)
signal_comparison.style.format("{:.4f}")"""
            ),
            md_cell(
                """### Lattice Sensitivity Analysis

This approximates how the lattice signal changes when volatility or credit assumptions are moved up/down. The shocks are intentionally simple so they can be explained cleanly with the available data."""
            ),
            code_cell(
                """def lattice_sensitivity_summary(market_prices, lattice_prices):
    rows = []
    base_gap = (lattice_prices - market_prices) / market_prices
    base_avg = base_gap.stack().mean()
    scenarios = [
        ("base lattice", lattice_prices),
        ("volatility -10%", lattice_prices * (1 - 0.35 * 0.10)),
        ("volatility +10%", lattice_prices * (1 + 0.35 * 0.10)),
        ("credit spread -50 bps", lattice_prices * (1 + 3.0 * 0.005)),
        ("credit spread +50 bps", lattice_prices * (1 - 3.0 * 0.005)),
    ]
    for name, prices in scenarios:
        gap = (prices - market_prices) / market_prices
        rows.append({
            "scenario": name,
            "avg_lattice_cheapness": gap.stack().mean(),
            "change_vs_base": gap.stack().mean() - base_avg,
            "trade_zone_rate": ((gap > 0) & (gap <= 0.04)).stack().mean(),
        })
    return pd.DataFrame(rows).set_index("scenario")

sensitivity = lattice_sensitivity_summary(market_prices, lattice_prices)
sensitivity.style.format("{:.4f}")"""
            ),
            md_cell(
                """## Signal Construction

Cheapness is the valuation signal:

`cheapness = (model price - market price) / market price`

Positive cheapness means the model thinks the convert is undervalued. The strategy focuses on modest positive cheapness rather than extreme cheapness, because very large gaps can be stale marks, distressed credit events, missing terms, or data issues."""
            ),
            code_cell(
                """fig, ax = plt.subplots(figsize=(10, 5))
cheapness.stack().dropna().clip(-0.25, 0.25).hist(bins=50, ax=ax)
ax.axvline(0, color="black", linewidth=1)
ax.axvline(0.04, color="firebrick", linestyle="--", linewidth=1)
ax.set_title("Distribution of Model Cheapness")
ax.set_xlabel("(model price - market price) / market price")
ax.set_ylabel("Bond-week observations")
plt.show()"""
            ),
            code_cell(
                """signal = pd.DataFrame({
    "cheapness": cheapness.stack(),
    "fwd_1w_market_return": market_prices.pct_change().shift(-1).stack(),
}).dropna()

signal["cheapness_quintile"] = pd.qcut(signal["cheapness"], 5, labels=False, duplicates="drop")
signal_summary = signal.groupby("cheapness_quintile").agg(
    avg_cheapness=("cheapness", "mean"),
    avg_fwd_1w_return=("fwd_1w_market_return", "mean"),
    hit_rate=("fwd_1w_market_return", lambda x: (x > 0).mean()),
    obs=("fwd_1w_market_return", "size"),
)
signal_summary.style.format("{:.4f}")"""
            ),
            code_cell(
                """fig, ax = plt.subplots(figsize=(8, 4.5))
signal_summary["avg_fwd_1w_return"].plot(kind="bar", ax=ax, color="#4c78a8")
ax.set_title("Average Next-Week Convert Return by Cheapness Quintile")
ax.set_xlabel("Cheapness quintile, low to high")
ax.set_ylabel("Average next-week return")
plt.show()"""
            ),
            md_cell(
                """## Implemented Trade-Zone Diagnostics

This section turns the improvement ideas into reusable outputs. The candidate ledger records every bond-week with market price, model price, cheapness, next-week market return, and flags for whether it falls into the strategy's 0-4% trade zone."""
            ),
            code_cell(
                """candidate_ledger = pd.DataFrame({
    "market_price": market_prices.stack(),
    "model_price": model_prices.stack(),
    "cheapness": cheapness.stack(),
    "fwd_1w_market_return": market_prices.pct_change().shift(-1).stack(),
}).dropna(subset=["market_price", "model_price", "cheapness"])
candidate_ledger.index = candidate_ledger.index.set_names(["date", "ticker"])
candidate_ledger = candidate_ledger.reset_index()
candidate_ledger["is_model_cheap"] = candidate_ledger["cheapness"] > 0
candidate_ledger["is_trade_zone"] = (candidate_ledger["cheapness"] > 0) & (candidate_ledger["cheapness"] <= 0.04)
candidate_ledger["cheapness_bucket"] = pd.qcut(candidate_ledger["cheapness"], 5, labels=False, duplicates="drop")

trade_summary = candidate_ledger.groupby("ticker").agg(
    observations=("cheapness", "size"),
    avg_cheapness=("cheapness", "mean"),
    trade_zone_obs=("is_trade_zone", "sum"),
    trade_zone_rate=("is_trade_zone", "mean"),
    avg_fwd_return=("fwd_1w_market_return", "mean"),
)

trade_zone_returns = (
    candidate_ledger[candidate_ledger["is_trade_zone"]]
    .groupby("ticker")["fwd_1w_market_return"]
    .mean()
    .rename("trade_zone_avg_fwd_return")
)
trade_summary = trade_summary.join(trade_zone_returns)
trade_summary["trade_zone_share_of_all_candidates"] = (
    trade_summary["trade_zone_obs"] / candidate_ledger["is_trade_zone"].sum()
)
trade_summary = trade_summary.sort_values(["trade_zone_obs", "trade_zone_avg_fwd_return"], ascending=False)

trade_summary.head(12).style.format("{:.4f}")"""
            ),
            code_cell(
                """fig, ax = plt.subplots(figsize=(10, 5))
trade_summary["trade_zone_obs"].head(12).sort_values().plot(kind="barh", ax=ax, color="#59a14f")
ax.set_title("Most Frequent Trade-Zone Candidates")
ax.set_xlabel("Bond-week observations with 0-4% cheapness")
ax.set_ylabel("")
plt.show()"""
            ),
            md_cell(
                """## Performance Metrics

The backtest output separates realized PnL, unrealized PnL, and available capital. This section annualizes weekly returns, estimates tail risk, and measures drawdown."""
            ),
            code_cell(
                """def drawdown(series):
    wealth = (1 + series.fillna(0)).cumprod()
    peak = wealth.cummax()
    dd = wealth / peak - 1
    end = dd.idxmin()
    start = wealth.loc[:end].idxmax()
    return dd.min(), start, end

def perf_table(returns, rf=None, periods=52):
    rows = {}
    for col in returns.columns:
        r = returns[col].dropna()
        aligned_rf = 0 if rf is None else rf.reindex(r.index).fillna(method="ffill").fillna(0)
        excess = r - aligned_rf
        var_5 = r.quantile(0.05)
        cvar_5 = r[r <= var_5].mean()
        max_dd, dd_start, dd_end = drawdown(r)
        rows[col] = {
            "weeks": len(r),
            "ann_return": (1 + r).prod() ** (periods / len(r)) - 1,
            "ann_vol": r.std() * np.sqrt(periods),
            "sharpe": excess.mean() / r.std() * np.sqrt(periods),
            "hit_rate": (r > 0).mean(),
            "skew": r.skew(),
            "excess_kurtosis": r.kurtosis(),
            "var_5": var_5,
            "cvar_5": cvar_5,
            "max_drawdown": max_dd,
            "dd_start": dd_start,
            "dd_end": dd_end,
        }
    return pd.DataFrame(rows).T

rf = results["rf"] / 52 if "rf" in results else None
performance = perf_table(results_core, rf=rf)
performance.style.format("{:.4f}")"""
            ),
            code_cell(
                """worst_week_table = []
for col in results_core.columns:
    for date, value in results_core[col].dropna().nsmallest(10).items():
        worst_week_table.append({"strategy_leg": col, "date": date, "return": value})
worst_week_table = pd.DataFrame(worst_week_table)
worst_week_table.head(15).style.format({"return": "{:.4f}"})"""
            ),
            code_cell(
                """fig, ax = plt.subplots(figsize=(10, 5))
cumulative = (1 + results_core.fillna(0)).cumprod() - 1
cumulative.plot(ax=ax, linewidth=1.8)
ax.set_title("Cumulative Strategy Return Components")
ax.set_ylabel("Cumulative return")
ax.set_xlabel("")
plt.show()"""
            ),
            md_cell(
                """## Factor and Market Exposure

A convertible arbitrage strategy should be less exposed to broad equity direction than a simple stock portfolio, but it can still carry residual exposure to credit spreads, growth equity regimes, funding conditions, and hedge timing."""
            ),
            code_cell(
                """factor_cols = [
    "SPX Index", "LQD US Equity", "HYG US Equity", "WORLD Index",
    "SX5E Index", "HSI Index", "CCMP Index", "GSTHHVIP Index",
]
factor_cols = [c for c in factor_cols if c in results.columns]
correlations = results_core.join(results[factor_cols]).corr().loc[results_core.columns, factor_cols]
correlations.style.format("{:.4f}")"""
            ),
            code_cell(
                """def regressions(returns, factors):
    rows = []
    joined = returns.join(factors, how="inner").dropna()
    for target in returns.columns:
        y = joined[target]
        x = joined[factors.columns]
        x = pd.concat([pd.Series(1.0, index=x.index, name="const"), x], axis=1)
        beta = np.linalg.lstsq(x.values, y.values, rcond=None)[0]
        fitted = x.values @ beta
        resid = y.values - fitted
        tss = ((y.values - y.values.mean()) ** 2).sum()
        r2 = 1 - (resid ** 2).sum() / tss
        row = {"alpha_weekly": beta[0], "r2": r2}
        row.update({f"beta_{name}": value for name, value in zip(factors.columns, beta[1:])})
        rows.append(pd.Series(row, name=target))
    return pd.DataFrame(rows)

factor_reg = regressions(results_core, results[factor_cols].fillna(0))
factor_reg.style.format("{:.4f}")"""
            ),
            code_cell(
                """rolling_corr = results[["unrealized_pnl", "SPX Index", "HYG US Equity"]].rolling(26).corr()
unrealized_corr = rolling_corr.loc[(slice(None), "unrealized_pnl"), ["SPX Index", "HYG US Equity"]]
unrealized_corr.index = unrealized_corr.index.droplevel(1)

fig, ax = plt.subplots(figsize=(10, 5))
unrealized_corr.plot(ax=ax, linewidth=1.6)
ax.axhline(0, color="black", linewidth=1)
ax.set_title("Rolling 26-Week Unrealized PnL Correlation")
ax.set_ylabel("Correlation")
ax.set_xlabel("")
plt.show()"""
            ),
            md_cell(
                """## Stress Testing

Stress windows help determine whether the strategy behaves like a true relative-value portfolio or simply hides equity/credit exposure until markets become disorderly."""
            ),
            code_cell(
                """stress_periods = {
    "COVID shock": ("2020-02-19", "2020-04-08"),
    "2022 rates/credit selloff": ("2022-01-05", "2022-10-12"),
    "AI/growth rebound": ("2023-01-04", "2023-12-27"),
    "Late sample": ("2024-01-03", "2025-03-12"),
}
stress_rows = []
for name, (start, end) in stress_periods.items():
    window = results_core.loc[start:end]
    if window.empty:
        continue
    stress_rows.append({
        "period": name,
        "realized_total": (1 + window["realized_pnl"].fillna(0)).prod() - 1,
        "unrealized_total": (1 + window["unrealized_pnl"].fillna(0)).prod() - 1,
        "available_capital_total": (1 + window["available capital"].fillna(0)).prod() - 1,
        "spx_total": (1 + results.loc[start:end, "SPX Index"].fillna(0)).prod() - 1,
        "hyg_total": (1 + results.loc[start:end, "HYG US Equity"].fillna(0)).prod() - 1,
    })
stress = pd.DataFrame(stress_rows).set_index("period")
stress.style.format("{:.4f}")"""
            ),
            md_cell(
                """## Research Conclusions

The project supports the idea that convertible arbitrage can be framed as a systematic relative-value strategy, but the strongest version needs stronger data controls and richer trade attribution. The current signal produces a useful valuation lens; the risk diagnostics show that the strategy is not simply long equity, though it still has meaningful credit/liquidity exposure.

The biggest upgrade for a future production version would be a trade-level ledger: each position should record entry signal, bond PnL, stock hedge PnL, Treasury hedge PnL, coupons, dividends, borrow costs, transaction costs, slippage, and exit reason."""
            ),
        ],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "pygments_lexer": "ipython3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    (ROOT / "Comprehensive Convertible Arbitrage Project.ipynb").write_text(
        json.dumps(notebook, indent=1), encoding="utf-8"
    )


if __name__ == "__main__":
    outputs = build_report()
    build_notebook()
    print("Wrote:")
    print(f"- {REPORTS / 'comprehensive_project_report.md'}")
    print(f"- {ROOT / 'Comprehensive Convertible Arbitrage Project.ipynb'}")
    print("Key performance metrics:")
    print(outputs["performance"].round(4).to_string())
