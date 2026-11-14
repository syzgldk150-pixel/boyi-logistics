"""WeChat personal-account bots over the official iLink Bot API (ClawBot).

``ilink`` is the HTTP client, ``tokens`` keeps bot tokens in the Agent's
private state directory, ``service`` runs the Console's QR binding and
revocation, and ``channel`` long-polls every bound device and hands verified
text to the shared chat pipeline injected by the composition root.
"""
