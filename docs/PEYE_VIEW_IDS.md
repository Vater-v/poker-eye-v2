# PokerEye view IDs

Coin Poker UI is React Native. The shipped `assets/index.android.bundle` is
Hermes **bytecode** (HBC v96, 33191308 bytes). Adding `testID={...}` in that
file would insert object keys and shift every jump target; the only safe
bundle patch we keep is the one-opcode AppUpdater disable.

The live Android view tree **is** the DOM the janitor taps. React Native
already reserves:

| resource | id |
|---|---|
| `react_test_id` | `0x7f0a02cd` |
| `view_tag_native_id` | `0x7f0a03b9` |

HmuriyBridge stamps `peye.*` into both slots on the clickable view (and dumps
them as `id=`). After the first frame, taps go by id, not by translated label.

## Catalog

| id | Control |
|---|---|
| `peye.cash_games` | Home / lobby Cash Games tile |
| `peye.multiway` | Multiway (not Heads Up, not NLH badge) |
| `peye.heads_up` | Heads Up chip — never join |
| `peye.join_similar` | Table chrome Join Similar |
| `peye.leave_table` | Menu item Leave / Exit table |
| `peye.confirm_yes` | Confirm leave-table dialog |
| `peye.table_closed` | TABLE CLOSED tab chip |
| `peye.hamburger` | Top-left table menu |
| `peye.action_fold` | Bottom Fold |
| `peye.action_check` | Bottom Check |
| `peye.action_call` | Bottom Call |
| `peye.action_raise` | Bottom Raise |

Do not invent new string matching for these controls. If a new button appears,
stamp a new `peye.*` id here first.
