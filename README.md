# EMESE

Volánbusz menetrend alapú indulás/érkezés API.


## Végpontok

```
GET /
GET /EMESE/{city}
GET /EMESE/{city}/stops?q=
GET /EMESE/{city}/departures?stop=&limit=
GET /EMESE/{city}/last-departed?stop=
GET /EMESE/{city}/eta?trip=&date=&stop=&delay_sec=
```

`{city}`: `Kisvarda` | `Nyiregyhaza`
