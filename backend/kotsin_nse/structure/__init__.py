"""Price structure on 5-minute candles: swings, breaks of structure, order blocks, fair value gaps,
liquidity and sweeps, and the levels of a setup. Pure functions over closed bars — the live engine,
the replay and the backtester all call :func:`~kotsin_nse.structure.engine.analyze`."""
