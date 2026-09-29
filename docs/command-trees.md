# Command trees

Prefix command groups share one overview helper, `NHCogs/command_overview.py`. Cog READMEs list the commands. This file is the shape those overviews follow.

## Overviews

A bare root group takes no arguments. It lists only its direct children and tells the moderator to open a category for the full list.

A bare nested group lists every visible command under it. In a private moderator channel it may also show current values. In a public channel it shows syntax only and says to run the command in a private moderator channel. Overview output does not ping anyone.

Hidden commands are left out of every overview.

Each listed line is `` `{prefix}{qualified name} {usage}` - {short help} ``. The public usage string wins over the callback parameter name. A nullable setting documents `[channel|clear]` even when the parameter is only named `channel`.

## Nullable values

A single nullable value is one public command:

- Omit the argument to show the current value.
- Pass the value to store it.
- Pass `clear` to remove it.

`set` is not a documented command. A hidden `set` subcommand may keep an older longer path working, and overviews do not list it.

If that command also has a real sibling, such as `reset`, the overview lists the value command and that sibling. `reset` restores a default. It is not `clear`.

An argument-free group is only a category. The overview lists its visible descendants instead of the group itself.

## What stays nested

Toggles stay their own nested command. Do not fold `toggle` into the parent.

Collections stay `add` and `remove` under an argument-free group, plus `rename` where a name can change.

A scalar that is only assigned stays a required-argument leaf. The parent overview is where its current value is shown.

A show-or-set leaf stays optional when checking that one setting is the normal action. Omitting the argument shows the value. It does not become a required `set`.
