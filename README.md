## Sonos Custom Integration with Group Volume Management 

Please test this per the instructions in the email. If anybody any issues, please let me know ASAP.  

### HACS Custom Repository Link
`https://github.com/TheMegamind/sonos_gv_test.git`

### Sonos Grouping Templates (Added here by Request)

#### Is Player Grouped (with >1 Members)?
```jinja
{% set members = state_attr('media_player.sonos_office', 'group_members') %}
{{ members is list and members|length > 1 }}
````

---

#### Is Player Group Coordinator (with >1 Members)?

```jinja
{% set members = state_attr('media_player.sonos_office', 'group_members') %}
{{ members is list and members|length > 1 and members[0] == 'media_player.sonos_office' }}
```

---

#### Is Player Group Coordinator with >1 Members and Playing

```jinja
{% set members = state_attr('media_player.sonos_office', 'group_members') %}
{{ members is list and members|length > 1 and members[0] == 'media_player.sonos_office'
   and is_state('media_player.sonos_office', 'playing') }}
```

```

This way your README will render nicely with clear sections and proper code highlighting.  

Want me to also add a short **example automation snippet** in the README showing how to use one of these inside a `condition:` block?
```
