import pandas as pd
import numpy as np
from arch import arch_model
import math
import datetime
pd.options.display.float_format = "{:,.4f}".format
from typing import Union, List
from pandas import Timestamp

import matplotlib.pyplot as plt
import seaborn as sns

import statsmodels.api as sm
from sklearn.linear_model import LinearRegression

import warnings
warnings.filterwarnings("ignore")

from collections import defaultdict

from scipy.stats import norm

import re

import matplotlib.pyplot as plt
import seaborn as sns

import warnings
warnings.filterwarnings("ignore")

from scipy.optimize import minimize
import scipy.stats as stats
from scipy.stats import norm
from scipy.optimize import newton
import quandl
import os
import datetime



def convert_to_years(col):
    if col.endswith('M'):
        return int(col[:-1]) / 12  # Convert months to years
    elif col.endswith('Y'):
        return int(col[:-1])  # Years remain the same
    else:
        raise ValueError(f"Unknown time format: {col}")


def calculate_ttm(dates, maturities):
    """
    Calculate the time to maturity (TTM) for each ticker at each date.

    Parameters:
    -----------
    dates : list or pandas.Series
        A list or Series of current dates (as strings or datetime objects).
    maturities : pandas.Series
        A Series of bond maturities indexed by ticker (as strings or datetime objects).

    Returns:
    --------
    pandas.DataFrame
        A DataFrame with dates as the index, tickers as columns, and TTMs as values.
    """
    # Convert dates and maturities to datetime if they are not already
    dates = pd.to_datetime(dates)
    maturities = pd.to_datetime(maturities)

    # Create a DataFrame to store the TTMs
    ttm_df = pd.DataFrame(index=dates)

    # Calculate TTM for each ticker
    for ticker, maturity in maturities.items():
        ttm_df[ticker] = (maturity - dates).days / 365

    return ttm_df


def compute_realized_vol(row):
    ticker = row.name  # Get the ticker name
    ttm_series = ttm_df[ticker]  # Get the TTM values for the given ticker
    rolling_vols = pd.Series(index=ttm_series.index, dtype=float)  # Placeholder for results
    
    for date in ttm_series.index:
        window_size = int(ttm_series.loc[date] * 252)  # Dynamic window size
        if window_size > 1 and date in daily_returns.index:
            idx = daily_returns.index.get_loc(date)  # Locate index of the date
            if idx >= window_size:
                rolling_vols.loc[date] = daily_returns[ticker].iloc[idx - window_size + 1: idx + 1].std() * np.sqrt(252)
    
    return rolling_vols


def bond_pricer(zcb, credit_spread, coupon_rate, tenor):
    """
    Calculate the price of a bond using a given zero-coupon bond (ZCB) curve.

    Parameters:
    -----------
    zcb : pandas.Series
        A zero-coupon bond yield curve, where the index represents maturities
        (in years) and the values represent continuously compounded yields.
    coupon_rate : float
        The annualized coupon rate of the bond (as a decimal).
    tenor : float
        The bond's maturity in years.

    Returns:
    --------
    float
        The present value (price) of the bond.

    Notes:
    ------
    - The bond pays semi-annual coupons, as indicated by the step size of 0.5 in the `times` array.
    - The function performs linear interpolation on the ZCB curve to estimate rates at required time points.
    - The bond price consists of two components:
      1. The present value of the face value discounted at the interpolated rate.
      2. The present value of semi-annual coupon payments discounted at the interpolated rates.
    """
    times = np.arange(tenor, 0, step=-0.5)[::-1]
    if times.shape[0]==0:
        p = 1.0
    else:
        r = np.interp(times, zcb.index.values, zcb.values) # Linear interpolation for risk-free rates
        discounts = r + credit_spread # Add credit spread to risk-free rates
        p = np.exp(-tenor*discounts[-1]) + 0.5 * coupon_rate * np.exp(-discounts*times).sum() # Present value of face value and coupon payments (ie. price of bond)
    return p * 1000


def calc_vol_thesis(df1, df2, weight=0.5):
    result = pd.DataFrame(index=df1.index, columns=df1.columns)
    for col in df1.columns:
        for idx in df1.index:
            vol1 = df1.at[idx, col]
            vol2 = df2.at[idx, col]
            if pd.notna(vol1) and pd.notna(vol2):
                # Combine variances and then take square root:
                result.at[idx, col] = np.sqrt(weight * vol1**2 + (1 - weight) * vol2**2)
            else:
                result.at[idx, col] = np.nan
    return result


def black_scholes(S, K, T, r, sigma, q, option_type="call"):
    """
    Black-Scholes option pricing formula with continuous dividend yield.
    """
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)

    if option_type == "call":
        price = S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    elif option_type == "put":
        price = K * np.exp(-r * T) * norm.cdf(-d2) - S * np.exp(-q * T) * norm.cdf(-d1)
    else:
        raise ValueError("option_type must be 'call' or 'put'")

    return price




def bond_yield(price, coupon_rate, years_to_maturity, rate, face_value = 1000, frequency=2):
    """
    Calculate the yield to maturity (YTM) of a bond given its price.

    Parameters:
    - price (float): The current market price of the bond.
    - face_value (float): The bond's face value (typically $100).
    - coupon_rate (float): The annual coupon rate as a decimal (e.g., 0.05 for 5%).
    - years_to_maturity (float): The number of years remaining until maturity.
    - frequency (int, optional): The number of coupon payments per year (default is 2 for semi-annual).

    Returns:
    - float: The yield to maturity (YTM) as a decimal.
    """

    periods = int(years_to_maturity * frequency)  # Total number of coupon payments
    coupon_payment = (coupon_rate * face_value) / frequency  # Coupon per period

    # Function to find the root (YTM)
    def price_function(y):
        discounted_coupons = sum(coupon_payment / (1 + y / frequency) ** (t + 1) for t in range(periods))
        discounted_face_value = face_value / (1 + y / frequency) ** periods
        return discounted_coupons + discounted_face_value - price

    # Initial guess for YTM
    initial_guess = 0.05  # 5% as a starting point

    # Solve for YTM using Newton's method
    try:
        ytm = newton(price_function, initial_guess)
    except RuntimeError:
        
        ytm = RATE

    return ytm




def calc_bond_duration(coupon_rate, ytm, years_to_maturity, frequency=2): # closed formula
    """
    Calculate the duration of a bond using the closed-form formula.

    Parameters:
    - coupon_rate (float): Annual coupon rate as a decimal (e.g., 0.05 for 5%).
    - ytm (float): Annual yield to maturity as a decimal (e.g., 0.04 for 4%).
    - frequency (int): Coupon payment frequency per year (e.g., 2 for semi-annual).
    - years_to_maturity (float): Time to maturity in years.

    Returns:
    - float: Duration of the bond.
    """
    # Calculate periodic yield (y_tilde), periodic coupon rate (c_tilde), and effective periods (tau_tilde)
    y_tilde = ytm / frequency
    c_tilde = coupon_rate / frequency
    tau_tilde = frequency * years_to_maturity

    # Compute the closed-form duration formula
    numerator = (1 + y_tilde) / y_tilde - (1 + y_tilde + tau_tilde * (c_tilde - y_tilde)) / (
        c_tilde * ((1 + y_tilde) ** tau_tilde - 1) + y_tilde
    )
    duration = (1 / frequency) * numerator

    return duration # this is a percentage, is Macauley duration (response to a parallel shift in the spot curve)


def black_scholes_greeks(S, K, T, r, sigma, q, option_type="call"):
    """
    Computes the Greeks: Delta, Gamma, Vega, Rho, Theta for a European option.
    """
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    
    pdf_d1 = norm.pdf(d1)  # pdf
    cdf_d1 = norm.cdf(d1)  # cdf
    cdf_d2 = norm.cdf(d2)

    # Greeks
    delta = np.exp(-q * T) * cdf_d1 if option_type == "call" else np.exp(-q * T) * (cdf_d1 - 1)
    gamma = np.exp(-q * T) * pdf_d1 / (S * sigma * np.sqrt(T))
    vega = S * np.exp(-q * T) * pdf_d1 * np.sqrt(T) / 100
    rho = (K * T * np.exp(-r * T) * cdf_d2 / 100) if option_type == "call" else (-K * T * np.exp(-r * T) * norm.cdf(-d2) / 100)
    theta = (- (S * sigma * np.exp(-q * T) * pdf_d1) / (2 * np.sqrt(T))
             - r * K * np.exp(-r * T) * cdf_d2
             + q * S * np.exp(-q * T) * cdf_d1) / 365 if option_type == "call" else (
            (- (S * sigma * np.exp(-q * T) * pdf_d1) / (2 * np.sqrt(T))
             + r * K * np.exp(-r * T) * norm.cdf(-d2)
             - q * S * np.exp(-q * T) * norm.cdf(-d1)) / 365)

    return {"Delta": delta, "Gamma": gamma, "Vega": vega, "Rho": rho, "Theta": theta}




def simulate_trading_strategy_final(market_prices, model_prices, delta_df_adjusted, equity_prices_resampled, 
                              duration_df, zcb_prices_df, div_yields, bond_des, zcb_rates,
                              initial_capital=100_000_000, leverage=3.0, trans_cost_rate=0.005, delta_threshold=0.0005,
                              min_position_size=2_000_000, max_position_size=8_000_000, 
                              max_cheapness=0.04, min_available_capital = 10_000_000):
    """"
    Simulates a trading strategy that goes long a convertible bond whose market price is cheap relative to its model price
    and hedges the delta risk by going short the underlying equity.

    Parameters:
    - market_prices (pd.DataFrame): A DataFrame of market prices for convertible bonds.
    - model_prices (pd.DataFrame): A DataFrame of model prices for convertible bonds.
    - delta_df_adjusted (pd.DataFrame): A DataFrame of delta values for convertible bonds.

    Returns:
    - bond_positions (pd.DataFrame): A DataFrame of trading positions for each convertible bond.
    - equity_positions (pd.DataFrame): A DataFrame of trading positions for each underlying equity.
    - pnl (pd.DataFrame): A DataFrame of daily P&L for each convertible bond.
    """
   # Make copies of the input DataFrames
    market_prices = market_prices.copy()
    model_prices = model_prices.copy()
    delta_df_adjusted = delta_df_adjusted.copy()
    equity_prices_resampled = equity_prices_resampled.copy()
    duration_df = duration_df.copy()
    zcb_prices_df = zcb_prices_df.copy()

    # Standardize missing values across each dataframe
    model_prices = model_prices.where(~market_prices.isna())
    delta_df_adjusted = delta_df_adjusted.where(~market_prices.isna())

    # Capital and PnL trackers
    available_capital_df = pd.DataFrame(index=market_prices.index, columns=['available capital'])
    unrealized_weekly_pnl_df = pd.DataFrame(index=market_prices.index, columns=market_prices.columns)
    realized_weekly_pnl_df = pd.DataFrame(index=market_prices.index, columns=market_prices.columns)
    open_positions_df = pd.DataFrame(index=market_prices.index, columns=['open positions'])

    # Initialize variables
    available_capital = initial_capital * leverage
    open_positions = {}
    dropped_tickers = {}
    stock_trans_cost_rate = trans_cost_rate/10 # Stock more liquid, thus lower transaction cost ########### ASSUMPTION
    treasury_trans_cost_rate = trans_cost_rate/20 # Treasury more liquid, thus lower transaction

    # Identify cheap trading opportunities
    cheapness = (model_prices - market_prices) / model_prices
    trade_mask = (market_prices < model_prices) & (cheapness <= max_cheapness) & (cheapness >= 0)

    for date in trade_mask.index:
        for ticker in trade_mask.columns:
            if (trade_mask.loc[date, ticker]) and (ticker not in open_positions.keys()) and (available_capital > min_available_capital):
                # TAKE A LONG POSITION IN THE CONVERTIBLE BOND
                # Scale position size (notional) depending on bond's cheapness
                bond_notional = (min_position_size + (cheapness.at[date, ticker] / max_cheapness) * (max_position_size - min_position_size))
                # Identify market price and calculate quantity of bonds to purchase
                bond_market_price = market_prices.at[date, ticker]
                bond_quantity = round(int(bond_notional / bond_market_price),-2)
                bond_notional = bond_quantity * bond_market_price
                # Transact if you have the capital
                if bond_notional > available_capital - min_available_capital:
                    continue
                # Account for convert transaction costs
                bond_transaction_cost = bond_notional * trans_cost_rate

                # DELTA HEDGE WITH STOCK (Hedge our delta risk by shorting the underlying equity)
                # Identify delta to calculate quantity of stock to short
                delta = delta_df_adjusted.at[date, ticker] # +
                equity_quantity = -delta * bond_quantity # -
                # Identify equity price to calculate equity notional
                equity_price = equity_prices_resampled.at[date, ticker] # +
                equity_notional = equity_quantity * equity_price # -
                # Account for stock transaction costs
                stock_transaction_cost = abs(equity_notional) * stock_trans_cost_rate


                # HEDGE DURATION WITH TREASURIES
                # Identify duration to calculate quantity of bond to short
                duration = duration_df.at[date, ticker] #+
                bond_hedge_quantity = -duration #-
                # Calculate the notional of the bond hedge
                bond_hedge_notional = zcb_prices_df.at[date, 1.0] * bond_hedge_quantity #-
                # Account for Treasury bond transaction costs
                treasury_trans_cost = abs(bond_hedge_notional) * treasury_trans_cost_rate #+

                # Adjust available capital after paying for bonds, shorting stock and Treasuries
                # from bond purchase
                available_capital = available_capital - bond_notional - bond_transaction_cost
                # from shorting stock
                available_capital = available_capital - stock_transaction_cost
                available_capital += abs(equity_notional) # (CHANGE)
                # from shorting Treasuries
                available_capital = available_capital - treasury_trans_cost
                available_capital += abs(bond_hedge_notional)
                
                available_capital_df.at[date, 'available capital'] = available_capital
                unrealized_weekly_pnl_df.at[date, ticker] = 0
                realized_weekly_pnl_df.at[date, ticker] = -(bond_transaction_cost + stock_transaction_cost)

                open_positions[ticker] = {'bond quantity': bond_quantity, # +
                                          'bond cost basis': bond_market_price, # +
                                          'bond price': bond_market_price, # +
                                          'bond purchase notional': bond_notional, # +
                                          'equity quantity': equity_quantity,# -
                                          'equity cost basis': equity_price, # +
                                          'equity price': equity_price, # +
                                          'stock short notional': equity_notional, # -
                                          'average short stock price': equity_price, # +
                                          'stock short proceeds': -equity_notional, # +
                                          'delta': delta, # +
                                          'bond hedge quantity': bond_hedge_quantity, # -
                                          'bond hedge price': zcb_prices_df.at[date, 1.0], #+
                                          'Treasury short notional': bond_hedge_notional} #-
                
                open_positions_df.at[date, 'open positions'] = len(open_positions)


            # If position is already open, need to check if we need to exit out of the position. If not, then hold and potentially rehedge
            elif ticker in open_positions.keys():
                # Account for the bond's coupon payments and stock's dividend payments
                accrued_dividend_payment = div_yields.at[date, ticker]/52 * equity_prices_resampled.at[date, ticker] * abs(open_positions[ticker]['equity quantity'])
                accrued_bond_coupon = bond_des.loc['Coupon Rate', ticker]/52 * 1000 * open_positions[ticker]['bond quantity']
                # Account for stock borrow costs
                funding_cost= zcb_rates.loc[date, 0.25] # Borrowing at 3m ZCB rate (from Treasury Yield curve)
                stock_rebate = 0.75 * funding_cost # Prime Brokerage kicks back interest (75% of the funding cost as) on your stock short cash proceeds 
                equity_notional = open_positions[ticker]['equity quantity'] * open_positions[ticker]['equity price']
                net_stock_borrow_cost = abs(equity_notional) * (funding_cost - stock_rebate)/52
                
                # Adjust available capital after accounting for accrued dividend payments, accrued bond coupon payments, and stock borrow costs
                available_capital = available_capital - accrued_dividend_payment + accrued_bond_coupon - net_stock_borrow_cost

                # Calculate weekly unrealized PnL
                unrealized_bond_pnl = open_positions[ticker]['bond quantity'] * (market_prices.at[date, ticker] - open_positions[ticker]['bond price'])
                unrealized_equity_pnl = open_positions[ticker]['equity quantity'] * (equity_prices_resampled.at[date, ticker] - open_positions[ticker]['equity price'])
                unrealized_hedge_bond_pnl = open_positions[ticker]['bond hedge quantity'] * (zcb_prices_df.at[date, 1.0] - open_positions[ticker]['bond hedge price'])
                unrealized_total_pnl = unrealized_bond_pnl + unrealized_equity_pnl + unrealized_hedge_bond_pnl
                unrealized_weekly_pnl_df.at[date, ticker] = unrealized_total_pnl

                # Update open positions dictionary with new prices
                open_positions[ticker]['bond price'] = market_prices.at[date, ticker]
                open_positions[ticker]['equity price'] = equity_prices_resampled.at[date, ticker]

                # Unwind the position and the hedge associated with it if 1. stock price > conversion price (convert goes ITM) 2. bond matures 3. last trading date in the sample
                #  When we sell out of the convert, remove it from the universe to prevent it from being traded again
                if pd.to_datetime(date) >= pd.Timestamp(bond_des.at['Maturity', ticker]):
                    # Collect principal
                    bond_principal_collection = open_positions[ticker]['bond quantity'] * 1000
                    # Close our stock hedge position -- buy back the stock
                    equity_buyback_cost = abs(open_positions[ticker]['equity quantity']) * equity_prices_resampled.at[date, ticker]
                    equity_buyback_trans_cost = abs(equity_buyback_cost) * stock_trans_cost_rate
                    net_equity_buyback_cost = equity_buyback_cost + equity_buyback_trans_cost
                    # Close out the bond hedge position -- buy back the Treasuries
                    bond_hedge_buyback_cost = abs(open_positions[ticker]['bond hedge quantity']) * zcb_prices_df.at[date, 1.0]
                    bond_hedge_buyback_trans_cost = abs(bond_hedge_buyback_cost) * treasury_trans_cost_rate
                    net_bond_hedge_buyback_cost = bond_hedge_buyback_cost + bond_hedge_buyback_trans_cost

                    # Adjust available capital after selling the bond and buying back the stock
                    available_capital = available_capital + bond_principal_collection - net_equity_buyback_cost - net_bond_hedge_buyback_cost # + (CHANGE) -- return collateral from cash proceeds here instead
                    available_capital_df.at[date, 'available capital'] = available_capital
                    
                    # Calculate PnL
                    bond_trade_pnl = bond_principal_collection - open_positions[ticker]['bond purchase notional']
                    stock_trade_pnl = abs(open_positions[ticker]['stock short notional']) - net_equity_buyback_cost
                    treasury_trade_pnl = abs(open_positions[ticker]['Treasury short notional']) - net_bond_hedge_buyback_cost
                    realized_weekly_pnl_df.at[date, ticker] = bond_trade_pnl + stock_trade_pnl + treasury_trade_pnl - accrued_dividend_payment + accrued_bond_coupon - net_stock_borrow_cost # (CHANGE)

                    # remove ticker from trading pool
                    dropped_tickers[ticker] = date
                    model_prices.drop(columns=[ticker], inplace=True)
                    market_prices.drop(columns=[ticker], inplace=True)
                    delta_df_adjusted.drop(columns=[ticker], inplace=True)
                    equity_prices_resampled.drop(columns=[ticker], inplace=True)
                    trade_mask.drop(columns=[ticker], inplace=True)
                    duration_df.drop(columns=[ticker], inplace=True)

                    open_positions.pop(ticker)
                    open_positions_df.at[date, 'open positions'] = len(open_positions)

                elif (market_prices.at[date, ticker] >= model_prices.at[date, ticker]) or date == market_prices.index[-1]:
                    # Sell out of the bond position
                    bond_sale_proceeds = open_positions[ticker]['bond quantity'] * market_prices.at[date, ticker]
                    bond_sale_trans_cost = abs(bond_sale_proceeds) * trans_cost_rate
                    net_bond_sale_proceeds = bond_sale_proceeds - bond_sale_trans_cost
                    # Close our stock hedge position -- buy back the stock
                    equity_buyback_cost = abs(open_positions[ticker]['equity quantity']) * equity_prices_resampled.at[date, ticker]
                    equity_buyback_trans_cost = abs(equity_buyback_cost) * stock_trans_cost_rate
                    net_equity_buyback_cost = equity_buyback_cost + equity_buyback_trans_cost
                    # Close out the bond hedge position -- buy back the Treasuries
                    bond_hedge_buyback_cost = abs(open_positions[ticker]['bond hedge quantity']) * zcb_prices_df.at[date, 1.0]
                    bond_hedge_buyback_trans_cost = abs(bond_hedge_buyback_cost) * treasury_trans_cost_rate
                    net_bond_hedge_buyback_cost = bond_hedge_buyback_cost + bond_hedge_buyback_trans_cost
                    
                    # Adjust available capital after selling the bond and buying back the stock
                    available_capital = available_capital + net_bond_sale_proceeds - net_equity_buyback_cost - net_bond_hedge_buyback_cost # + (CHANGE) -- return collateral from cash proceeds here instead
                    available_capital_df.at[date, 'available capital'] = available_capital

                    # Calculate PnL
                    bond_trade_pnl = net_bond_sale_proceeds - open_positions[ticker]['bond purchase notional']
                    stock_trade_pnl = abs(open_positions[ticker]['stock short notional']) - net_equity_buyback_cost
                    treasury_trade_pnl = abs(open_positions[ticker]['Treasury short notional']) - net_bond_hedge_buyback_cost
                    realized_weekly_pnl_df.at[date, ticker] = bond_trade_pnl + stock_trade_pnl + treasury_trade_pnl - accrued_dividend_payment + accrued_bond_coupon - net_stock_borrow_cost # (CHANGE)

                    open_positions.pop(ticker)
                    open_positions_df.at[date, 'open positions'] = len(open_positions)

                
                # If conditions to sell out of the position are not met, then hold and check if we need to rehedge
                else:
                    bond_hedge_buyback_cost = abs(open_positions[ticker]['bond hedge quantity']) * zcb_prices_df.at[date, 1.0] * (1 + treasury_trans_cost_rate)
                    available_capital = available_capital - bond_hedge_buyback_cost
                    duration = duration_df.at[date, ticker] #+
                    bond_hedge_quantity = -duration #-
                    bond_hedge_notional = zcb_prices_df.at[date, 1.0] * bond_hedge_quantity #-
                    treasury_trans_cost = abs(bond_hedge_notional) * treasury_trans_cost_rate #+
                    available_capital = available_capital - treasury_trans_cost # updated
                    available_capital += abs(bond_hedge_notional) # updated
                    open_positions[ticker]['bond hedge quantity'] = bond_hedge_quantity # -
                    open_positions[ticker]['bond hedge price'] = zcb_prices_df.at[date, 1.0] #+

                    # Check if we need to rehedge the stock position due to changes in delta
                    new_delta = delta_df_adjusted.at[date, ticker]
                    old_delta = open_positions[ticker]['delta']
                    delta_diff = new_delta - old_delta
                    equity_price = equity_prices_resampled.at[date, ticker]
                    # If delta has moved past a certain threshold, then rehedge
                    if abs(delta_diff) > delta_threshold * bond_des.loc['Conversion Ratio', ticker]:
                        # If delta has increased, short more stock. If delta has decreased, buy back stock.
                        add_equity_quantity = -delta_diff * bond_quantity
                        add_equity_notional = add_equity_quantity * equity_price
                        stock_transaction_cost = abs(add_equity_notional) * stock_trans_cost_rate
                        available_capital = available_capital - stock_transaction_cost + abs(add_equity_notional) # (CHANGE)
                        open_positions[ticker]['equity cost basis'] = (open_positions[ticker]['equity cost basis'] * abs(open_positions[ticker]['equity quantity']) + abs(add_equity_notional)) / (abs(open_positions[ticker]['equity quantity']) + abs(add_equity_quantity))
                        open_positions[ticker]['equity quantity'] += add_equity_quantity
                        open_positions[ticker]['stock short notional'] += add_equity_notional
                        open_positions[ticker]['delta'] = new_delta

                        available_capital = available_capital + abs(add_equity_notional) - stock_transaction_cost
                        stock_realized_pnl = stock_transaction_cost

                    
                    available_capital_df.at[date, 'available capital'] = available_capital
                    other_realized_pnl = -accrued_dividend_payment + accrued_bond_coupon - net_stock_borrow_cost
                    realized_weekly_pnl_df.at[date, ticker] = stock_realized_pnl + other_realized_pnl
                    open_positions_df.at[date, 'open positions'] = len(open_positions) 

        available_capital_df.at[date, 'available capital'] = available_capital

    return {'available_capital_df': available_capital_df,
            'unrealized_weekly_pnl_df': unrealized_weekly_pnl_df,
            'realized_weekly_pnl_df': realized_weekly_pnl_df,
            'open_positions_df': open_positions_df}



def downside_beta(asset_returns, market_returns):
    """
    Calculate the downside beta of an asset relative to the market.
    
    Parameters:
    asset_returns (pd.Series): The returns of the asset.
    market_returns (pd.Series): The returns of the market.

    Returns:
    float: Downside beta of the asset.
    """
    # Ensure the data is aligned
    asset_returns, market_returns = asset_returns.align(market_returns, join='inner')

    # Select only negative market returns
    mask = market_returns < 0
    market_downside = market_returns[mask]
    asset_downside = asset_returns[mask]

    # Check if there are enough negative returns to compute downside beta
    if len(market_downside) == 0:
        return np.nan  # Return NaN if no downside movements

    # Compute downside beta
    covariance = np.cov(asset_downside, market_downside)[0, 1]
    variance = np.var(market_downside)

    if variance == 0:
        return np.nan  # Avoid division by zero

    downside_beta_value = covariance / variance
    return downside_beta_value