---
name: browser-forms
description: Fill a whole web form, post a comment or reply, or read a long thread in Safari or Chrome with the fewest Mac MCP browser calls.
---
# Browser forms, comments and long pages

Mac MCP has one-action shortcuts for the browser work agents repeat most. Use them
instead of observing and acting one field or one step at a time.

## Fill a form: one browser_act action

1. `browser_observe` the tab once. When it sees form fields it returns `fill_template`,
   a ready `{type:'fill', values:{...}}` action with every field label.
2. Put the values in and send it as one `browser_act`:
   `{type:'fill', values:{'Full name': 'Ada', 'Country': 'Turkey', 'I accept the terms': true}}`
   - Text boxes are typed; native and custom dropdowns pick the option; checkboxes and
     radios take true or false and are left alone when already right.
   - Fields go in order. A field that appears after an earlier choice (country, then state)
     is awaited.
   - A field that is not found is listed in `failed_fields`; the others are already filled.
     Fill only the missing ones next time.
3. Do not press Submit unless the task asks for it.

## Post a comment or reply: one action

`{type:'post', text:'...'}` for the page's comment box, or
`{type:'post', text:'...', within:'a phrase only that comment has'}` for a reply. It opens the
reply box with that comment's own Reply button, types, presses the box's submit button and
confirms the text is on the page.
- `posted: true` means it is published.
- `POST_NOT_CONFIRMED` (`posted: null`) means the button was pressed but the text did not show
  up: check the page before posting again; never post twice blindly.

## Read a long page or thread

`browser_observe` reads what is on screen. To read a whole thread, send one
`browser_act` with `{type:'scan'}`: it scrolls and collects the items in one call.
