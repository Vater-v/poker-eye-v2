# Protocol Specification — "NLH4 HU 2 hands.pcap"

Reference capture: `poker-eye-v2\raw\NLH4 HU 2 hands.pcap` (329,754 bytes)
Analyzed 2026-08-17. Read-only analysis; this document records observed facts (F) and inferences (I).

---

## 0. Executive summary

This capture is **not** a SmartFox SFS2X stream. It is a localhost TCP capture of a
**PokerEYE-style hook channel** that mirrors the traffic of the **pppoker / PokerBros**
Android app (`com.lein.pppoker.android`) to a local hint backend, and the hint
("cc") commands the backend sends back.

- Link layer: Linux cooked v1 (DLT_LINUX_SLL, linktype 113), all traffic on loopback.
- One TCP conversation: `127.0.0.1:35400 ↔ 127.0.0.1:17770`.
- Wire framing (both directions): **4-byte big-endian length prefix + UTF-8 JSON body**.
  There is no `0x80` header byte and no 2-byte length anywhere in the capture.
- The game protocol visible inside the hook channel is **pppoker protobuf** (`pb.*`
  commands) plus pppoker's JSON-over-WebSocket layer (command name `WEB`).
- There are **no `game.*` commands** in this capture. The `game.user_turn`,
  `game.user_action`, `game.game_init`, `game.seat`, `game.reset_data` command names
  assumed by the legacy code do not occur. The pppoker equivalents are documented below.
- Hint/CC/EYE traffic **is present**: backend → client `{"tag":"cc", ...}` action
  recommendations (CHECK/FOLD/RAISE/CALL) and client → backend `{"tag":"broadcast", ...}`
  confirmations.
- The game is **single-board NLH heads-up** (2 players, seats 2 and 5). No double-board
  / PLO multi-board markers exist. Two complete hands are contained.

---

## 1. File format (F)

| Item | Value |
|---|---|
| Magic | `0xA1B2C3D4` — classic PCAP, little-endian, microsecond timestamps |
| Version | 2.4 |
| Snaplen | 262144 |
| Link type | 113 = DLT_LINUX_SLL (Linux cooked capture v1, 16-byte packet header) |
| Packets | 1092 (all IPv4/TCP, all on loopback) |
| Duration | ~275 s (ts_sec range 1786958964–1786959239) |

Packet record: 16-byte header (`ts_sec`, `ts_usec`, `incl_len`, `orig_len`) + data.
Each data blob starts with a 16-byte SLL header (proto field at offset 14–15 = `0x0800`).

---

## 2. IP/port pairs and roles (F)

Exactly **one** TCP conversation in the whole capture:

| Endpoint | Role (I) | Traffic volume |
|---|---|---|
| `127.0.0.1:35400` | PokerEYE hook client (inside/next to the game) | 532 payloads / 235,819 bytes → 17770 |
| `127.0.0.1:17770` | PokerEYE hint backend | 15 payloads / 2,167 bytes → 35400 |

The real game server is **not** in the capture; it is referenced in a `WEB` login
message: `gserver_ip: global-entry.cozypoker.net`, `gserver_port: 4000`,
`platform: Brazil`, client public IP `201.4.123.90` (F). The game client is a mobile
browser/WebView: user agent `Mozilla/5.0 (Linux; Android 11; V2180GA) Chrome/87.0.4280.141`
(F, from the `browser_profile` frame).

### 2.1 Frame encoding on the hook channel (F)

Every TCP payload begins `00 00 xx xx` followed by JSON. The framing is:

```
[uint32 BE: length N][N bytes: UTF-8 JSON]
```

Verified on all 547 non-empty payloads: first payload byte of every segment is `0x00`
(the high byte of the BE length), and `len(payload) == 4 + N` for every frame extracted.
Frame sizes observed: 60 B – 2.7 KB.

### 2.2 Client → backend frame schema (F)

```json
{"data": "",
 "msg": "<JSON string: captured game event>",
 "packageName": "com.lein.pppoker.android",
 "tag": "traffic" | "broadcast"}
```

The inner `msg` JSON has the keys (F):
`timestamp` (ms), `cmd`, `direction`, `pid`, `uid`, `data`, `location`, `seq`.

- `cmd == "WEB"`: pppoker WebSocket **text** frame; `data` is the parsed JSON object
  (version check, login handshake, `{"code":0,"msg":"Ok","trace_id":...}` acks).
- `cmd` starts with `pb.`: pppoker WebSocket **binary** frame carrying a protobuf
  message; `data` is the **base64-encoded** protobuf bytes.
- `tag == "broadcast"` (7 frames): hook echoes the hint action it displayed, e.g.
  `{"action":"show_message","message":"check","time":4000}`.

**Direction caveat (F + I):** the `direction` field is literally `"ServerToClient"`
on all 527 traffic frames (7 more have no direction). This is not trustworthy —
e.g. `pb.UserLoginREQ` clearly carries client-originated data (device id, version
`4.2.155`, `"android"`, `127.0.0.1:4000`, `"Brazil"`). **True direction must be
inferred from the pppoker naming convention: `*REQ` = client→server, `*RSP` =
server→client response, `*BRC` = server→client broadcast.** The hook evidently logs
the full duplex stream but only ever stamps `ServerToClient`.

### 2.3 Backend → client frame schema (F)

15 frames, same 4-byte-BE-length + JSON framing:

| tag | count | purpose |
|---|---|---|
| `browser_profile` | 1 | target device UA / emulator profile |
| `game_mode` | 7 | `{"tag":"game_mode","data":"auto","packageName":"com.lein.pppoker.android"}` — enables auto-play |
| `cc` | 7 | hint / click-command from the EYE backend (see §7) |

---

## 3. SmartFox SFS2X framing — REFUTED for this capture (F)

The legacy assumption “frames start with `0x80` + 2-byte big-endian length” does **not**
hold for this capture:

- No TCP payload begins with `0x80`. The per-direction first-byte histograms are
  `0x00` for 532/532 client segments and 15/15 backend segments (the high byte of the
  4-byte BE length prefix).
- The payloads are self-describing UTF-8 JSON, not SmartFox's
  header(0x80|0x60|0x70)+length+binary-object framing. No SmartFox object-encoding
  markers (`0x80`/`0x81` string/int short-form) appear.
- The inner game protocol is protobuf (`pb.*`), which SmartFox SFS2X never uses for
  its extension commands.

Conclusion: **this reference capture must be parsed with the JSON length-prefix +
protobuf decoder described here**, not the SmartFox parser.

---

## 4. Observed command inventory (F)

`cmd` histogram from 527 `traffic` frames (counts per distinct cmd):

### 4.1 Transport / session
| cmd | count | dir (I) |
|---|---|---|
| `WEB` | 31 | S→C (JSON-over-WebSocket: version check, entry login, acks) |
| `pb.HeartBeatREQ` | 82 | C→S |
| `pb.HeartBeatRSP` | 82 | S→C |

### 4.2 Lobby / meta (S→C unless *REQ)
`pb.UserLoginREQ/RSP`, `pb.ClubListREQ/RSP`, `pb.SelfUserInfoREQ`,
`pb.MoneyREQ/RSP`, `pb.DiamondREQ/RSP`, `pb.MailREQ/RSP`, `pb.NewMailNumREQ/RSP`,
`pb.ClubInfoREQ/RSP`, `pb.ClubConfigREQ/RSP`, `pb.ClubActivityInfoREQ/RSP`,
`pb.ClubStormInfoREQ/RSP`, `pb.ClubItemListREQ/RSP`, `pb.ClubRecommendOfficialRoomREQ/RSP`,
`pb.JackPotREQ/RSP`, `pb.JackPotConfigREQ/RSP`, `pb.NewPPCoinFlowREQ/RSP`,
`pb.EmojiPackageREQ/RSP`, `pb.GetRedPointDataREQ/RSP`, `pb.HandMissionStopStatusREQ/RSP`,
`pb.GetUserSettingREQ/RSP`, `pb.HandReviewUrlREQ/RSP`, `pb.UserCroupierInUsingREQ/RSP`,
`pb.RebateREQ/RSP`, `pb.StoreReviewTimeREQ/RSP`, `pb.GoldDiscountREQ/RSP`,
`pb.SelUserInfoRSP`, `pb.SelUserShopInfoREQ/RSP`, `pb.UserLocationRSP`,
`pb.GetTaskListREQ/RSP`, `pb.GetUserAIReportREQ/RSP`, `pb.QueryRewardREQ/RSP`,
`pb.OfficialMttPopupREQ/RSP`, `pb.SelUserVipInfoREQ/RSP`, `pb.PPCoinRSP`,
`pb.UserTryToGetNewPictureFrameREQ/RSP`, `pb.UserGetPictureFrameRedPointREQ/RSP`,
`pb.VerifyMailBindREQ`, `pb.GetUserAllMarksREQ/RSP`, `pb.UserPictureFrameListRSP`,
`pb.ReportGPS`, `pb.ClubUpdateBRC` — 1–9 occurrences each.

### 4.3 Room / seat lifecycle
| cmd | count | notes |
|---|---|---|
| `pb.CheckUserRoomREQ/RSP` | 9/9 | room existence check |
| `pb.ClubRoomREQ/RSP` | 9/9 | room list |
| `pb.EnterRoomREQ/RSP` | 4/4 | room entry; RSP carries table config + seat/user list |
| `pb.BookSeatREQ/RSP` | 1/1 | reserve seat |
| `pb.BookSeatBRC` | 4 | seat booked broadcast (f1=seat, f2=user info) |
| `pb.SitDownREQ/RSP` | 1/1 | sit down |
| `pb.SitDownBRC` | 2 | player seated broadcast (stack size, seat) |
| `pb.StandUpBRC` | 1 | player left |
| `pb.TotalBuyinBRC` | 2 | stack update (uid, chips, delta) |
| `pb.OtherEnterRoomBRC` | 1 | other player entered |
| `pb.OtherLeaveRoomBRC` | 5 | other player left |
| `pb.WaitListInfoBRC` / `pb.WaitListSeatInfoREQ` | 5/1 | wait list |

### 4.4 In-game (the “game.*” equivalents) — see §5
`pb.RoundStartBRC` (6), `pb.HandCardRSP` (2), `pb.ActionBRC` (17),
`pb.ActionNotifyBRC` (13), `pb.RoundOverBRC` (8), `pb.WinnerRSP` (2),
`pb.ShowHandRSP` (1), `pb.RoundHintMultipleTableREQ/RSP` (1/7),
`pb.FinishRoundHintRSP` (7), `pb.ZoomFoldBRC` (6), `pb.TableGameOverRSP` (1),
`pb.ChipsBackBRC` (1), `pb.ExchangeChipsRSP` (1), `pb.RebuyNotifyRSP` (1),
`pb.ClubRoomCountdownBRC` (1), `pb.SquidMarkInfoBRC` (2), `pb.DealerInfoRSP` (2).

There is **no** `game.*` namespace and no `game.user_turn` / `game.user_action` /
`game.game_init` / `game.seat` / `game.reset_data` in the capture (F).

---

## 5. Game message structures (F = observed wire fields; semantics marked I)

### 5.1 Card encoding (I, strongly supported)

Cards are 16-bit values: **`card = suit * 256 + rank`**, `suit ∈ {1,2,3,4}`,
`rank ∈ {2..14}` (14 = Ace). Observed examples decode to legal ranks (2–14) with
suit byte ≤ 4:

| raw | suit | rank | card |
|---|---|---|---|
| 1030 | 4 | 6 | 6♠4 (6 of suit 4) |
| 517 | 2 | 5 | 5 of suit 2 |
| 263 | 1 | 7 | 7 of suit 1 |
| 520 | 2 | 8 | 8 of suit 2 |
| 778 | 3 | 10 | T of suit 3 |
| 260 | 1 | 4 | 4 of suit 1 |
| 1034 | 4 | 10 | T of suit 4 |
| 1035 | 4 | 11 | J of suit 4 |
| 780 | 3 | 12 | Q of suit 3 |
| 262 | 1 | 6 | 6 of suit 1 |
| 270 | 1 | 14 | A of suit 1 |
| 1029 | 4 | 5 | 5 of suit 4 |
| 1031 | 4 | 7 | 7 of suit 4 |
| 521 | 2 | 9 | 9 of suit 2 |
| 770 | 3 | 2 | 2 of suit 3 |

(Exact suit→spade/heart/club/diamond mapping is not derivable from the capture alone.)

### 5.2 `pb.HandCardRSP` — hero hole cards (S→C) (F)

2 occurrences, 10 bytes each:
```
f1 (varint) = card1        e.g. 1030 (6s4)
f2 (varint) = card2        e.g.  517 (5s2)
f8 (varint) = 1            (constant)
f9 (varint) = 0            (constant)
```
Hand 1 hero: **6s4 5s2** (1030, 517). Hand 2 hero: **7s1 8s2** (263, 520).
Hero = uid **13765731**, nickname `Kalaytano`, seat **2**. Villain = uid
**13765357**, nickname `Letsggooo`, seat **5** (F from `pb.SitDownBRC`/`EnterRoomRSP`).

### 5.3 `pb.RoundStartBRC` — street transition / board reveal (S→C) (F)

`f1` = street counter, `f2` = repeated board cards for this street, `f3` = 2 (constant
in every observed instance), `f6` = embedded sub-message re-stating hero hole cards
(present on later streets only):

| f1 | meaning (I) | f2 observed |
|---|---|---|
| 1 | preflop | absent (0 cards) |
| 2 | flop | 3 cards |
| 3 | turn | 1 card |
| 4 | river | 1 card |

Examples (F):
```
RoundStartBRC f1=1 f3=2                                        # preflop
RoundStartBRC f1=2 f2={778,260,1034} f3=2 f6={1030,517,8:1,9:0}  # flop T,4,T
RoundStartBRC f1=3 f2={1029} f3=2 f6={263,520,8:1,9:0}          # turn 5
RoundStartBRC f1=4 f2={1031} f3=2 f6={263,520,8:1,9:0}          # river 7
```
Note: in hand 1 the turn/river were not delivered via `RoundStartBRC` (hero folded on
the flop) but appeared later inside `pb.WinnerRSP.f3`.

### 5.4 `pb.ActionBRC` — an action taken by a player (S→C broadcast) (F)

9–11 bytes, four varint fields:
```
f1 = seatId        (2 = hero, 5 = villain)
f2 = actionType    (see table below)
f3 = varint        (context-dependent; see notes)
f4 = varint        (amount / remaining stack; see notes)
```
All 17 observed in chronological order (F):
```
seat5 f2=8  f3=2  f4=158    # villain posts SB
seat2 f2=9  f3=4  f4=796    # hero posts BB
seat5 f2=3  f3=2  f4=156    # villain calls
seat2 f2=2  f3=0  f4=796    # hero checks (preflop)
seat2 f2=2  f3=0  f4=796    # hero checks (flop)
seat5 f2=7  f3=4  f4=152    # villain bets
seat2 f2=1  f3=0  f4=796    # hero folds
seat2 f2=8  f3=2  f4=798    # hero posts SB
seat5 f2=9  f3=4  f4=160    # villain posts BB
seat2 f2=4  f3=8  f4=790    # hero raises
seat5 f2=3  f3=6  f4=154    # villain calls
seat5 f2=7  f3=20 f4=134    # villain bets (flop)
seat2 f2=3  f3=20 f4=770    # hero calls
seat5 f2=7  f3=60 f4=74     # villain bets (turn)
seat2 f2=3  f3=60 f4=710    # hero calls
seat5 f2=7  f3=74 f4=0      # villain checks (river, amount 0)
seat2 f2=3  f3=74 f4=636    # hero checks/calls (river)
```
Action type map (I, correlated with the backend cc hints, which give ground-truth
hero decisions — see §7):
**1 = fold, 2 = check, 3 = call, 4 = raise, 7 = bet (0 amount = check), 8 = post SB,
9 = post BB.** (5 = all-in and 6 = ? not observed.)

`f3` matches the previous street's bet size (2, 4, 20, 60, 74 — the “price” context);
`f4` decreases with the player's stack and appears to be the **remaining stack after
the action** for that player (hero: 800 → 796 BB → 796 → 796 → 790 raise → 770 → 710
→ 636; villain: 160 → 156 → 154 → 152 → 134 → 74 → 0) — consistent, but not 100%
provable without the `.proto`. Chip scale: SB = 2, BB = 4 chips (I).

### 5.5 `pb.ActionNotifyBRC` — “whose turn it is” (S→C) (F)

5 varint fields, 11 bytes:
```
f1 = seatId whose turn it is
f2 = amount to call (0 if no bet)          (I)
f3 = pot / total-to-call reference         (I)
f4 = opponent stack reference              (I)
f5 = 2 (constant; 1 on the final river notify)
```
All 13 observed (F), hero turn (f1=2) instances in **bold**:
```
seat5 f2=2  f3=6   f4=798 f5=2   # after blinds (hand 1)
**seat2 f2=0 f3=4 f4=156 f5=2**   # preflop, hero to act
**seat2 f2=0 f3=4 f4=156 f5=2**   # flop, hero to act
seat5 f2=0  f3=4   f4=796 f5=2
**seat2 f2=4 f3=8 f4=156 f5=2**   # hero faces 4-chip bet
**seat2 f2=2 f3=6 f4=162 f5=2**   # hand 2 preflop, hero (SB) to act
seat5 f2=6  f3=12  f4=796 f5=2    # after hero raise
seat5 f2=0  f3=4   f4=790 f5=2    # hand 2 flop, villain first
**seat2 f2=20 f3=40 f4=154 f5=2** # hero faces 20-chip bet
seat5 f2=0  f3=4   f4=770 f5=2    # turn, villain first
**seat2 f2=60 f3=120 f4=134 f5=2**# hero faces 60-chip bet
seat5 f2=0  f3=4   f4=710 f5=2    # river, villain first
**seat2 f2=74 f3=148 f4=74 f5=1** # hero faces 74-chip bet (f5=1)
```
Hero-turn detection: `f1 == heroSeat`. This is the functional replacement for the
legacy `game.user_turn` (whose fields `whoseTurn/turnTime/callAmount/userTurnOptions/
initTimeStamp` do not exist here; closest mapping: f1↔whoseTurn, f2↔callAmount,
f5↔“options version/flag”, no timer field present in these messages).

### 5.6 `pb.RoundOverBRC` — end of street/hand (S→C) (F)

1 varint field: `f1 = cumulative pot` (I). Observed values per hand:
hand 1: 8 (end of preflop), 8 (hand over, hero folded);
hand 2: 20 (preflop), 60 (flop), 180 (turn), 328 (river/showdown).
(Values grow monotonically with street; also two empty `RoundOverBRC` 0-field frames
act as pre-round cleanup between hands.)

### 5.7 `pb.WinnerRSP` — hand result (S→C) (F)

```
f1 (sub) = winner entry: f1=seat, f2=0, f3=win amount, f4=hand category (5=straight),
           f6=winner uid, f16=?
f2 (sub, repeated) = per-seat pots: f1=seat, f2=signed amount (twos-complement), ...
f3 (sub) = remaining community cards (turn/river), repeated f1=card, f2=street/flag
f5 (sub, repeated) = per-seat result: f1=seat, f2=uid, f3=signed net chips
f8 (sub) = all seats: f1=0, repeated f2=uid
f9 = 4
```
Hand 2 example: winner seat 2 (hero) category 5 (straight), villain nets −160,
hero nets +154, community cards f3={1035 (J♠4), 780 (Q♠3)}.

### 5.8 `pb.ShowHandRSP` — showdown cards (S→C) (F)

```
f1 (sub, repeated per player): f1=seat, f2=card1, f3=card2
f3 = 0
```
Hand 2: seat 2 shows {263 (7s1), 520 (8s2)}, seat 5 shows {521 (9s2), 770 (2s3)}.

### 5.9 `pb.RoundHintMultipleTableRSP` / `pb.FinishRoundHintRSP` (F)

Appear right after each `ActionNotifyBRC` that is the hero's turn. The RSP is the
game's **built-in** “hint” feature echo (table id `10988681`, club id `4978860`,
`f7=20`, table name `Unnamed Table`, club `Club_06bb2e9fb8`, f10=6, f11=3, f12=5).
`FinishRoundHintRSP.f1 = 10988681` (table id). These are part of the pppoker traffic
and are **not** the external PokerEYE channel.

---

## 6. Session / game identity facts (F)

| Item | Value |
|---|---|
| Game app | `com.lein.pppoker.android` v4.2.155 (PokerBros/pppoker) |
| Client | Android 11 webview emulator, geo “Brazil”, public IP 201.4.123.90 |
| Hero | uid 13765731, “Kalaytano”, seat 2, buy-in 800 chips |
| Villain | uid 13765357, “Letsggooo”, seat 5, buy-in 160 chips |
| Club / table | club id 4978860 `Club_06bb2e9fb8`, table `Unnamed Table`, room strings `260817155659-10983765-...`, `260817173050-10988670-...`, `260817173203-10988711-...` |
| Format | **NLH heads-up, single board**, blinds 2/4 chips (I) |

---

## 7. Hint / CC / EYE (PokerEYE backend) traffic (F)

Backend → client (`17770 → 35400`), 7 `cc` frames, one per hero decision point:

```json
{"tag":"cc","data":"{\"type\":\"CHECK\",\"subtype\":0,\"delay\":4912,\"lifetime\":4000,\"amount\":0.0,\"message\":\"check\",\"arguments\":\"{}\"}","packageName":"com.lein.pppoker.android"}
```

| # | type | delay (ms) | amount | message | correlated hero action (I) |
|---|---|---|---|---|---|
| 1 | CHECK | 4912 | 0.0 | check | hand 1 preflop: check |
| 2 | CHECK | 3026 | 0.0 | check | hand 1 flop: check |
| 3 | FOLD | 0 | 0.0 | fold | hand 1 flop: fold to bet |
| 4 | RAISE | 2856 | 8.0 | raise 0.08 | hand 2 preflop: raise |
| 5 | CALL | 603 | 20.0 | call | hand 2 flop: call 20 |
| 6 | CALL | 2854 | 60.0 | call | hand 2 turn: call 60 |
| 7 | CALL | 1393 | 74.0 | call | hand 2 river: call 74 |

Client → backend confirmations (7 `broadcast` frames) echo each hint:
`{"action":"show_message","message":"<same>","time":4000}` (F).

`cc` schema (F): `type` (CHECK|FOLD|RAISE|CALL), `subtype` (0), `delay` (ms before
acting), `lifetime` (4000 ms display time), `amount` (float, currency units — the
cc amounts 8.0/20.0/60.0/74.0 are 100× the chip amounts 8/20/60/74), `message`
(human-readable), `arguments` (JSON string, always `{}` here).

The `game_mode` frames (`data:"auto"`) toggle auto-execution. `browser_profile`
registers the emulator UA with the backend. This is the complete EYE channel in the
capture; no other hint request/response type (no EYE “hint request” frame) appears.

---

## 8. Double-board / PLO check — CONFIRMED SINGLE-BOARD (F)

- Streets follow strict NLH structure: 0/3/1/1 board cards at preflop/flop/turn/river
  (`RoundStartBRC.f2`), one community sequence per hand.
- `HandCardRSP` carries exactly **2** hole cards (NLH), never 4 (PLO).
- No message contains two parallel board arrays or board-index fields; `WinnerRSP`
  community continuation contains only the 2 remaining single-board cards.
- `ShowHandRSP` shows exactly 2 hole cards per player.
- Both hands in the capture resolve on one 5-card board (hand 1: T 4 T J Q — hero
  folded flop; hand 2: 6 4 A 5 7 — hero straight wins at showdown).
- No PLO/double-board identifiers appear in `EnterRoomRSP` table config
  (f35=3, f37=2, f103=5 observed but constant across both HU tables).

**This capture is a single-board NLH heads-up reference.** Any multi-board parsing
must be driven by a different capture type.

---

## 9. Decoded hands (F = messages, I = narrative)

**Hand 1** (table `10983765`): hero 6s4 5s2 (SB posted by villain seat 5, BB by hero
seat 2). Preflop: villain calls, hero checks. Flop **T 4 T** (778/260/1034): hero
checks, villain bets 4, hero **folds** (cc: CHECK→CHECK→FOLD). WinnerRSP: villain
wins pot 8; board completes T 4 T **J Q** (1035/780) in the result frame.

**Hand 2** (table `10988681`): hero 7s1 8s2 (hero SB seat 2, villain BB seat 5).
Preflop: hero **raises** (cc RAISE 8), villain calls. Flop **6 4 A** (262/260/270):
villain bets 20, hero **calls** (cc CALL 20). Turn **5** (1029): villain bets 60,
hero **calls** (cc CALL 60). River **7** (1031): villain checks, hero calls/checks
(cc CALL 74). Showdown: hero 7-8 vs villain 9-2; hero wins pot 328 with an 8-high
straight (WinnerRSP category 5). Net: hero +154, villain −160 (TotalBuyinBRC: hero
804 chips).

---

## 10. Implications for the parser

1. **Drop the SmartFox framing.** Parse the channel as 4-byte-BE-length + JSON.
2. Read `msg.cmd`; if `pb.*`, base64-decode `msg.data` and decode with a generic
   protobuf wire-format walker (field numbers as documented in §5 — no `.proto` file
   is available/needed for these messages).
3. Derive direction from the REQ/RSP/BRC suffix, not the `direction` field.
4. Hero turn = `pb.ActionNotifyBRC` with `f1 == heroSeat` (seat 2 in this capture);
   hero hole cards = `pb.HandCardRSP.f1/f2`; board = accumulate `RoundStartBRC.f2`
   per street (and `WinnerRSP.f3` if hand ends early); actions = `pb.ActionBRC`
   (f1 seat, f2 action type 1/2/3/4/7/8/9, f4 amount).
5. Hint consumption: read `tag:"cc"` frames from the backend direction and
   `tag:"broadcast"` confirmations from the client direction.
6. Card decode: `suit = c >> 8`, `rank = c & 0xFF` (rank 2..14, 14=A).

*Prepared by protocol analysis subagent — facts marked (F) are directly observed in
the capture; items marked (I) are inferences with supporting evidence noted inline.*
