# Convertible Bond Arbitrage Research Project

This project studies a systematic convertible bond arbitrage strategy. The core idea is to identify convertible bonds that appear cheap relative to model fair value, buy the cheap converts, and hedge major market exposures with short underlying equity and Treasury exposure.

## Project Structure

- `Comprehensive Convertible Arbitrage Project.ipynb`  
  Clean reproducible notebook for the final research workflow.

- `Comprehensive Convertible Arbitrage Project.executed.ipynb`  
  Executed notebook with rendered outputs.

- `reports/comprehensive_project_report.md`  
  Main written report.

- `reports/figures/`  
  Generated charts used by the report.

- `reports/tables/`  
  Generated CSV diagnostics, including performance metrics, factor regressions, signal ledgers, and model comparisons.

- `scripts/build_comprehensive_project.py`  
  Rebuilds the report, figures, tables, and clean notebook from the project data.

- `data/`  
  Bloomberg-derived inputs and generated model/backtest CSVs.

## Methodology

The project compares two convertible bond valuation approaches:

1. Baseline model: straight bond value plus Black-Scholes value of the embedded conversion option.
2. Enhanced model: simplified credit-adjusted binomial convertible lattice.

The binomial lattice uses available inputs from the dataset: stock prices, conversion ratios, coupon and maturity terms, Treasury rates, CDS spreads, dividend yields, and volatility. It improves on the baseline by allowing the value at each node to be the maximum of continuation value and immediate conversion value.

## Main Outputs

The report includes:

- Convertible universe and data coverage audit.
- Baseline versus lattice pricing comparison.
- Baseline versus lattice signal-quality comparison.
- Lattice sensitivity analysis for volatility and credit assumptions.
- Candidate-level signal ledger and trade-zone diagnostics.
- Performance, drawdown, tail-risk, and stress-period analysis.
- Correlation and multi-factor exposure analysis.

## Rebuild Instructions

From the repository root:

```bash
python scripts/build_comprehensive_project.py
```

To execute the notebook:

```bash
jupyter nbconvert --to notebook --execute "Comprehensive Convertible Arbitrage Project.ipynb" --output "Comprehensive Convertible Arbitrage Project.executed.ipynb"
```

## Notes and Limitations

The lattice model is intentionally simplified because the current dataset does not include issuer call schedules, investor put schedules, make-whole tables, recovery assumptions, or conversion restriction windows. The current saved data supports candidate-level signal analysis; full executed-position cash-flow attribution would require the simulator to export every entry, rehedge, coupon, dividend, borrow-cost, transaction-cost, and exit event.

