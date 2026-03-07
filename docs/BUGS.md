## On split, it's not possible to edit the timestamps

To reproduce:

* I start `aw-watcher-afk-prompt --test-dialog`
* I press the "split"-button
* I enter "foo" and "bar" for descriptions
* For the second item, I change the start time from 17:12 to 17:02
* Minutes does not get adjusted
* Logs:

```
$ aw-watcher-afk-prompt --test-dialog 
2026-03-07 17:27:38 [INFO ]: Activity 0 description updated to: 'f'  (aw_watcher_afk_prompt.split_dialog:822)
2026-03-07 17:27:39 [INFO ]: Activity 0 description updated to: 'fo'  (aw_watcher_afk_prompt.split_dialog:822)
2026-03-07 17:27:39 [INFO ]: Activity 0 description updated to: 'foo'  (aw_watcher_afk_prompt.split_dialog:822)
2026-03-07 17:27:41 [INFO ]: Activity 1 description updated to: 'b'  (aw_watcher_afk_prompt.split_dialog:822)
2026-03-07 17:27:41 [INFO ]: Activity 1 description updated to: 'ba'  (aw_watcher_afk_prompt.split_dialog:822)
2026-03-07 17:27:41 [INFO ]: Activity 1 description updated to: 'bar'  (aw_watcher_afk_prompt.split_dialog:822)
2026-03-07 17:28:13 [INFO ]: Activity 1 start time changed to 17:02  (aw_watcher_afk_prompt.split_dialog:871)
2026-03-07 17:28:13 [WARNING]: Error parsing start time '17:2': Adjusted duration would make last activity less than 1 minute  (aw_watcher_afk_prompt.split_dialog:894)
2026-03-07 17:28:16 [INFO ]: Activity 1 start time changed to 17:02  (aw_watcher_afk_prompt.split_dialog:871)
2026-03-07 17:28:16 [WARNING]: Error parsing start time '17:02': Adjusted duration would make last activity less than 1 minute  (aw_watcher_afk_prompt.split_dialog:894)
```

But the "less than 1 minutes" is not correct.  "foo" started at 16:57.

This is an issue not only when testing the dialog, but also in production.  I haven't been able to adjust the timestamps for ages.
