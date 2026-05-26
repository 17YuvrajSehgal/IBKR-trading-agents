"""
Profit-protecting trailing-stop agent.

A cross-cutting agent that watches every open position in the account
and ratchets a stop-loss into the profit zone as price moves favorably.
Strategy-agnostic: it doesn't care which agent opened the position.
"""
