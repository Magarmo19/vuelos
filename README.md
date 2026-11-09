# Vuelos ✈️💛

Sigue en directo **toda la flota de VistaJet** y destaca el avión en el que va ella. Detecta despegues y aterrizajes, guarda su historial con estadísticas y publica un calendario con **solo sus vuelos**, al que te suscribes desde el iPhone o el Mac.

Todo es gratis y funciona solo:

- **GitHub Actions** pregunta cada minuto a tres redes ADS-B públicas (adsb.lol, airplanes.live y adsb.fi) por todos los Global y Challenger del mundo, y se queda con los que vuelan con indicativo **VJT**. La flota se va completando sola.
- **GitHub Pages** sirve la web y el calendario `vuelos.ics`.
- Los datos se guardan en una rama aparte llamada `datos`. Esa rama se reescribe en cada pasada, así el repositorio no engorda.

## Puesta en marcha desde el Mac (≈10 min)

1. **Crea el repositorio y sube los archivos.** Tiene que ser **público**, porque GitHub Pages y Actions sin límite son gratis solo en repos públicos. Usa un nombre neutro, por ejemplo `vuelos`.

   Con la app `gh` (`brew install gh`, y después `gh auth login`):

   ```bash
   cd vuelos
   git init -b main && git add . && git commit -m "Vuelos"
   gh repo create vuelos --public --source . --push
   ```

   Sin `gh`: crea el repo vacío en github.com y luego:

   ```bash
   cd vuelos
   git init -b main && git add . && git commit -m "Vuelos"
   git remote add origin https://github.com/TU-USUARIO/vuelos.git
   git push -u origin main
   ```

2. **Activa la web con Actions:** *Settings → Pages → Build and deployment → Source: **GitHub Actions***.
3. **Lanza la primera pasada:** *Actions → Rastrear flota → Run workflow*. Desde ahí se ejecuta sola cada ~5 minutos.
4. Cuando termine (unos 5 min), abre `https://TU-USUARIO.github.io/vuelos/`. En el iPhone, usa Safari → *Compartir → Añadir a pantalla de inicio*.
5. **Calendario:** *Ajustes → Suscribirse en Calendario*. En el iPhone puedes elegir la frecuencia de actualización en *Ajustes → Calendario → Cuentas → Calendarios suscritos*.

## Decir en qué avión va (lo importante)

Las asignaciones están en `asignaciones.json`: matrícula y fechas de cada rotación. De ahí salen sus vuelos, el calendario y el avión destacado en amarillo en el mapa.

**Desde la web (recomendado).** En *Flota* toca el avión y pulsa **«💛 Ella va en este avión»**. Se asigna desde hoy y la asignación anterior se cierra ayer. En *Ajustes* puedes añadir o editar rotaciones con fechas exactas, también futuras, si ya sabes su roster. Además:

- **En un avión de la flota:** toca un vuelo de su historial para marcarlo como suyo.
- **En «Sus vuelos»:** toca un vuelo y pulsa **«No iba en este vuelo»** para quitarlo.

Para guardar desde la web, la página necesita un token de GitHub (solo la primera vez en cada dispositivo):

1. Ve a github.com → *Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token*.
2. En *Repository access* elige *Only select repositories* y marca `vuelos`.
3. En *Permissions → Repository permissions*, pon **Contents: Read and write**.
4. Pega el token en *Ajustes → Edición desde la web*. Se guarda solo en ese dispositivo. Si lo pierdes, lo revocas en GitHub.

**Sin token:** edita `asignaciones.json` directamente en GitHub. Al guardar, el rastreador se relanza solo.

```json
{
  "etiqueta": "Ella",
  "asignaciones": [
    { "matricula": "9H-ILV", "hex": "4d210e", "desde": "2026-11-10", "hasta": null, "nota": "" }
  ],
  "incluir_vuelos": [],
  "excluir_vuelos": []
}
```

`etiqueta` es el nombre que aparece en la web y en el calendario. Cámbialo si quieres.

## Ajustes avanzados (`config.json`)

- `operador.prefijos_callsign`: indicativos que identifican la flota (`VJT`).
- `operador.tipos`: tipos de avión a consultar (GL7T, GLEX, GL5T, CL60, CL35, CRJ2…). Si VistaJet incorpora otro modelo, añade su código ICAO.
- `siempre_rastrear`: aviones que se siguen siempre por su código hex, aunque vuelen con otro indicativo.
- `dias_historial_flota`: días de historial que se guardan de toda la flota (30 por defecto). Sus vuelos se guardan siempre.

## Qué esperar

- **Retraso:** con la web abierta, los datos se refrescan cada minuto y suelen ir 1–5 minutos por detrás. GitHub a veces retrasa las ejecuciones programadas, así que puede haber huecos ocasionales de 10–15 minutos.
- **Destino:** en aviación privada no hay plan de vuelo público. El destino se sabe **al aterrizar**. Durante el vuelo verás por dónde va.
- **«~» delante de una hora:** es una hora estimada. Pasa cuando el despegue o el aterrizaje cae entre consultas o fuera de cobertura.
- **Flota incompleta al principio:** un avión aparece la primera vez que vuela con indicativo VJT. En unos días estará casi toda.
- **Privacidad:** el repo y la web son públicos para quien tenga el enlace (la web no aparece en buscadores). Solo hay matrículas y aeropuertos, que ya son públicos en Flightradar24. No añadas nombres reales, pasajeros ni nada de su trabajo.
- **Uso de las APIs:** son gratuitas para uso personal y no comercial. Respeta sus condiciones. adsb.lol ha anunciado que en el futuro pedirá una clave a quien no aporte datos.

## Estructura

```
index.html                    la web (pestañas Ella · Flota · Ajustes)
config.json                   operador, tipos de avión, aviones fijos
asignaciones.json             en qué avión va ella y cuándo
tracker/track.py              rastreador (Python, sin dependencias)
tracker/publicar.sh           guarda los datos en la rama "datos"
tracker/airports.json         ~5.000 aeropuertos (OurAirports, dominio público)
.github/workflows/rastrear.yml  tarea programada + despliegue de la web
```
