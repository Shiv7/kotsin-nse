"""The minute scanner: which F&O stocks are moving one way, how hard, and with whom.

Pure functions over bars (``scan/strength.py``, ``scan/board.py``). The live engine and a replay of
any past day call the same code, so a board seen on Friday afternoon can be rebuilt minute by minute
from the history cache and compared.
"""
