"""Traders whose wallets are public knowledge, to be followed (hype/traders.py).

Where the list came from (read 2026-11-21): the public leaderboard of labelled Solana trader
wallets at kolscan.io, which shows each trader's name, wallet and 30-day profit. Kept: traders
high on its monthly board with a few hundred trades in the month and at least four wins in
ten, plus a handful of long-standing names from its directory. Each name and wallet pairing
was also found on a second public tracker, and every wallet was then checked on the blockchain
itself: all 40 had traded within the previous day.

Cupsey is here twice because two wallets are publicly labelled as theirs. Two names that were
asked for have no publicly labelled Solana wallet and so cannot be followed: Brez (Brez Scales)
and TJR. Coins named after a trader are not that trader's wallet.

A place on this list is not a recommendation. Several of these wallets trade thousands of
times a month and are in and out of a coin within a minute, long before anyone following
could act. What each one is worth to a follower is measured here (the record on the page),
and a wallet can simply stop being used, so the list wants refreshing every few months.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Trader:
    name: str
    wallet: str


KNOWN: tuple[Trader, ...] = (
    Trader("Cupsey", "2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f"),
    Trader("Cupsey (old wallet)", "suqh5sHtr8HyJ7q8scBimULPkPpA557prMG47xCHQfK"),
    Trader("xander", "B3wagQZiZU2hKa5pUCj6rrdhWsX3Q6WfTTnki9PjwzMh"),
    Trader("decu", "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"),
    Trader("ban", "EqiFgyNw6kgrmYstWyrP8VjKhka7XEmKTZzHSmwpr1Zb"),
    Trader("trunoest", "ardinRsN1mNYVeoJWTBsWeYeXvuR9UUDGMsCDKpb6AT"),
    Trader("Wugi", "862TYSvRYoiHAK3F3WwTRYAfuGiQaGdxedN9AGvRGWo2"),
    Trader("Schoen", "5hAgYC8TJCcEZV7LTXAzkTrm7YL29YXyQQJPCNrG84zM"),
    Trader("gr3g", "J23qr98GjGJJqKq9CBEnyRhHbmkaVxtTJNNxKu597wsA"),
    Trader("The Doc", "DYAn4XpAkN5mhiXkRB7dGq4Jadnx6XYgu8L5b3WGhbrt"),
    Trader("Megga", "H31vEBxSJk1nQdUN11qZgZyhScyShhscKhvhZZU3dQoU"),
    Trader("West", "JDd3hy3gQn2V982mi1zqhNqUw1GfV2UL6g76STojCJPN"),
    Trader("Johnson", "J9TYAsWWidbrcZybmLSfrLzryANf4CgJBLdvwdGuC8MB"),
    Trader("Shawty", "387FRwow6MKDhSwuMULhKjqeXuxoBY4BbbaSntdk9LaH"),
    Trader("AlxCooks", "89HbgWduLwoxcofWpmn1EiF9wEdpgkNDEyPjzZ72mkDi"),
    Trader("Sheep", "78N177fzNJpp8pG49xDv1efYcTMSzo9tPTKEA9mAVkh2"),
    Trader("Nyhrox", "6S8GezkxYUfZy9JPtYnanbcZTMB87Wjt1qx3c6ELajKC"),
    Trader("Jijo", "4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk"),
    Trader("clukz", "G6fUXjMKPJzCY1rveAE6Qm7wy5U3vZgKDJmN1VPAdiZC"),
    Trader("WheresLodo", "6FWhUybWmnjsQ933v2r6o7bhEG5yH9g9XsV7NVWKj5EW"),
    Trader("Limfork.eth", "BQVz7fQ1WsQmSTMY3umdPEPPTm1sdcBcX9sP7o6kPRmB"),
    Trader("Hien", "Gn9jrmzjYPfAFsXpauik2FkkPZ9bSReAffKiMQR1qeZb"),
    Trader("Tom", "CEUA7zVoDRqRYoeHTP58UHU6TR8yvtVbeLrX1dppqoXJ"),
    Trader("Idontpaytaxes", "2T5NgDDidkvhJQg8AHDi74uCFwgp25pYFMRZXBaCUNBH"),
    Trader("Daumen", "8MaVa9kdt3NW4Q5HyNAm1X5LbR8PQRVDc1W8NMVK88D5"),
    Trader("Killua", "95TWoKkvv2z85EXBzd2z6NwbxH2f54AvwuCA9x3NoKep"),
    Trader("leyko", "Ab2baaggUWJMsVUtCvpRjq6iBRDk2Lxr5qzicHueHxM1"),
    Trader("big bags bobby", "8oQoMhfBQnRspn7QtNAq2aPThRE4q94kLSTwaaFQvRgs"),
    Trader("Gfree", "4yo9CUuTBbds9NFhZd4MzPiZZkUvveXdTnAH8qMsE8ku"),
    Trader("EustazZ", "FqamE7xrahg7FEWoByrx1o8SeyHt44rpmE6ZQfT7zrve"),
    Trader("AsyncDuck", "FeYVZPsDkmg3D9ddcySxLqGvAR9ES29ABZkeRcUn4FfY"),
    Trader("radiance", "FAicXNV5FVqtfbpn4Zccs71XcfGeyxBSGbqLDyDJZjke"),
    Trader("Mr. Frog", "4DdrfiDHpmx55i4SPssxVzS9ZaKLb8qr45NKY9Er9nNh"),
    Trader("Cented", "CyaE1VxvBrahnPWkqm5VsdCvyS2QmNht2UFrKJHga54o"),
    Trader("theo", "Bi4rd5FH5bYEN8scZ7wevxNZyNmKHdaBcvewdPFxYdLt"),
    Trader("dv", "BCagckXeMChUKrHEd6fKFA1uiWDtcmCXMsqaheLiUPJd"),
    Trader("Marcell", "FixmSpsBa7ew26gWdiqpoMAgKRFgbSXFbGAgfMZw67X"),
    Trader("Gake", "DNfuF1L62WWyW3pNakVkyGGFzVVhj4Yr52jSmdTyeBHm"),
    Trader("Scharo", "4sAUSQFdvWRBxR8UoLBYbw8CcXuwXWxnN8pXa4mtm5nU"),
    Trader("Iced", "DrJ6SnDXkEsPeGdmSs93v5rwWumv5QMvAGSZjAyWSd5o"),
)
