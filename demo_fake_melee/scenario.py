"""What the demo shows: which tournaments exist, where each one starts, and
which invented players the two demo servers follow.

Each tournament replays one captured fixture. `cast` gives a demo player the
results of whoever finished the fixture's Swiss rounds in that place, so a
low number is a player who does well.
"""

# Where each demo player finishes, per tournament.
TOURNAMENTS = [
    {
        # Already under way: bind it, watch it, advance it round by round.
        "id": 900101,
        "name": "Northgate Open",
        "fixture": 393247,
        "starts_in_hours": -3,
        "round": 2,
        "phase": "done",
        "cast": {
            "Kestrel_VX": 5, "mossgoblin": 18, "NovaTide": 33,
            "QuietRook": 60, "Brambleback": 110, "ohm_sweet_ohm": 170,
        },
    },
    {
        # Not started: discovery suggests it, and "Bind & watch" waits for round 1.
        "id": 900102,
        "name": "Midsummer Invitational",
        "fixture": 452461,
        "starts_in_hours": 48,
        "round": 0,
        "phase": "paired",
        "cast": {
            "Kestrel_VX": 41, "mossgoblin": 3, "NovaTide": 150,
            "QuietRook": 12, "Brambleback": 27, "ohm_sweet_ohm": 8,
        },
    },
    {
        # Second-to-last Swiss round just paired: ready for /topcut.
        "id": 900103,
        "name": "Harbour City Championship",
        "fixture": 455838,
        "starts_in_hours": -26,
        "round": 11,
        "phase": "paired",
        "cast": {
            "Kestrel_VX": 2, "mossgoblin": 7, "NovaTide": 9,
            "QuietRook": 12, "Brambleback": 20, "ohm_sweet_ohm": 45,
        },
    },
]

# One roster per demo server, in GUILD_IDS order: (melee.gg username, display name).
# QuietRook is on both, under a different display name in each.
ROSTERS = [
    [("Kestrel_VX", "Kes"), ("mossgoblin", None), ("NovaTide", "Nova"), ("QuietRook", "Rook")],
    [("QuietRook", "Quiet Rook"), ("Brambleback", "Bram"), ("ohm_sweet_ohm", None)],
]
