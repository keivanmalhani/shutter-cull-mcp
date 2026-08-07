# shutter-cull-mcp

[![CI](https://github.com/keivanmalhani/shutter-cull-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/keivanmalhani/shutter-cull-mcp/actions/workflows/ci.yml)
![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)

[English](README.md) | Espanol

![Demo de shutter-cull-mcp: el servidor se niega a arrancar sin una lista explicita de raices permitidas](docs/demo.gif)

Un servidor MCP que deja a un agente de IA hacer el culling de tu sesion de fotos, y que no puede irse con ella.

Envuelve a [shutter-cull](https://github.com/keivanmalhani/shutter-cull), que evalua nitidez, ojos abiertos y estetica y escribe los picks como sidecars XMP que Lightroom lee de forma nativa. El motor ya era seguro de correr. Entregarselo a un agente es otro problema, y este repo es la respuesta a ese problema, no un envoltorio RPC delgado sobre un CLI.

## El problema que resuelve

[shutter-mcp](https://github.com/keivanmalhani/shutter-mcp) es de solo lectura, y toda su historia de seguridad es esa frase. Agregarle escritura habria costado esa garantia. Asi que la escritura vive aqui, con su propia envoltura.

Un agente que puede escribir en una biblioteca de fotos es una clase de riesgo distinta a uno que solo puede leerla. Puede ser inyectado por el nombre de un archivo. Puede alucinar una carpeta. Puede llamar a una herramienta de la que le hablaron pero que nunca le mostro a nadie. Y un argumento `confirm: true` no defiende nada, porque un modelo dispuesto a llamar la herramienta tambien esta dispuesto a mandar `true`.

Por eso aqui escribir no es una herramienta que el agente llama. Es un plan que el agente debe producir, mostrar y luego citar de vuelta.

## Cinco compuertas

```text
  propose_picks ......... solo lectura. Devuelve un plan y un plan_id.
        |
        |  el agente le muestra el plan al humano
        v
  apply_picks(plan_id, confirm) .... rechaza salvo que TODO se cumpla:
        |
        |   1  el plan_id lo emitio ESTE proceso en ejecucion
        |   2  el plan no ha expirado
        |   3  nada bajo el cambio desde que se propuso
        |   4  confirm es la frase exacta "apply N changes"
        |   5  cada cuadro sigue resolviendo dentro de una raiz permitida
        v
  sidecars escritos .... y registrados, asi undo_last_apply los revierte
```

**1. El plan_id no se puede falsificar.** Es un sha256 sobre la raiz y sobre la ruta, decision, tamano y mtime de cada cuadro. Un agente no puede construirlo, adivinarlo ni traerlo de otra sesion. Un servidor reiniciado no honra ninguno.

**2. Los planes expiran.** Treinta minutos, solo en memoria, nunca escritos a disco.

**3. La obsolescencia se revisa al momento de escribir.** Entre que un humano aprueba un plan y la escritura ocurre, la biblioteca puede moverse. Si algun cuadro fue editado, reemplazado o borrado en esa ventana, la escritura se rechaza y se nombran los archivos. No se escribe nada parcial.

**4. La confirmacion no es un booleano.** Debe ser la cadena literal que imprimio el plan, `apply 12 changes`, con el numero coincidiendo. Citar de vuelta el alcance del cambio es evidencia barata de que el plan si se leyo. La confirmacion refleja es un modo de falla real en agentes y un booleano la invita.

**5. La lista de raices permitidas se fija al arrancar.** El servidor se lanza con `--root` y se niega a arrancar sin una. Ninguna herramienta la amplia. Las rutas se resuelven a traves de symlinks antes de la prueba de contencion, asi que un enlace que vive dentro de la lista y apunta afuera se rechaza por donde aterriza. La contencion se vuelve a revisar justo antes de escribir, porque un symlink puede cambiarse en el intervalo.

Y `apply_picks` no recibe argumento de carpeta. Un plan solo puede aplicarse a la carpeta contra la que se propuso, porque no hay donde poner otra.

## Todo es reversible

Cada sidecar se fotografia antes de tocarlo. `undo_last_apply` restaura cada uno exactamente y borra los que no existian antes. Un sidecar que algo mas edito despues de la escritura se deja en paz y se reporta por nombre en lugar de sobrescribirse en silencio.

Dos limites honestos: el undo es de un solo paso y no sobrevive a un reinicio del servidor. Persistir el registro significaria que este servidor escriba archivos que nadie le pidio escribir, lo cual es peor trato que perder el undo entre reinicios.

## Lo que nunca hace

- Nunca abre un archivo de imagen original para escritura. Solo `.xmp` al lado de los originales, la misma postura no destructiva que Lightroom usa para metadatos de RAW.
- Nunca toca una ruta fuera de la lista permitida.
- Nunca escribe sin un plan que se le mostro a un humano.
- Nunca hace una llamada de red, salvo la descarga de modelo opcional y verificada por checksum del propio shutter-cull.

## Instalacion

```bash
pip install git+https://github.com/keivanmalhani/shutter-cull-mcp.git
```

El motor se instala aparte, porque la envoltura de seguridad se puede leer y auditar por si sola:

```bash
pip install git+https://github.com/keivanmalhani/shutter-cull.git
```

```bash
brew install exiftool
```

`server_status` reporta honestamente cuando falta el motor, en lugar de fallar de forma misteriosa mas tarde.

## Uso

```bash
shutter-cull-mcp --root ~/Pictures/2026-shoots
```

Repite `--root` para mas carpetas. En la configuracion de un cliente MCP:

```json
{
  "mcpServers": {
    "shutter-cull": {
      "command": "shutter-cull-mcp",
      "args": ["--root", "/Users/you/Pictures/2026-shoots"]
    }
  }
}
```

## Herramientas

| Herramienta | Escribe | Que hace |
| --- | --- | --- |
| `propose_picks` | no | Corre el pipeline completo en solo lectura y devuelve un plan mas un `plan_id`. |
| `apply_picks` | **si** | Escribe los sidecars de un plan ya propuesto, detras de las cinco compuertas. |
| `undo_last_apply` | si | Revierte la ultima aplicacion exactamente. |
| `explain_pick` | no | Por que un cuadro recibio su decision. Un cuadro que el motor dejo en paz no tiene entrada, y esa ausencia es la respuesta. |
| `list_plans` | no | Planes vivos, el mas nuevo primero. Si un plan no aparece, no se puede aplicar. |
| `server_status` | no | Las raices permitidas, que son el limite duro de todo lo que ocurre aqui. |

## Una sesion

El servidor responde en ingles, asi que el bloque siguiente es su salida real,
sin traducir. La frase de confirmacion la genera el codigo como
`apply N changes`, por lo que traducirla haria que este documento mintiera
sobre lo que hay que escribir.

```text
propose_picks(root="~/Pictures/canyon-2026")

  Cull plan for /Users/keivan/Pictures/canyon-2026
  412 frames analyzed.
  38 picks, 61 rejects, 313 left alone.
  ...
  plan_id: 8e2806e0ed2bfa8930c05349089e586a
  To apply, call apply_picks with that plan_id and confirm="apply 99 changes".

apply_picks(plan_id="8e28...", confirm="true")
  Refused: Confirmation did not match. To apply this plan the confirm
  argument must be exactly "apply 99 changes". This is deliberately not a
  boolean: quoting the change count back is evidence the plan was read.

apply_picks(plan_id="8e28...", confirm="apply 99 changes")
  Applied. 99 sidecars written: 38 picks, 61 rejects.
  Only .xmp sidecars were written. No original image file was opened for
  writing. Call undo_last_apply to reverse this.
```

Lo que ocurre ahi: el plan se propone y no escribe nada. El primer intento de
aplicarlo se rechaza porque `confirm="true"` no es la frase exacta, y un
booleano no demuestra que alguien haya leido el plan. El segundo intento cita
el numero de cambios y si escribe, solo archivos `.xmp` adjuntos, nunca la
imagen original, y `undo_last_apply` lo revierte.

## Desarrollo

```bash
pip install -e ".[dev]"
pytest
```

51 pruebas, sin motor: la suite corre contra un motor falso a proposito. De lo que esta hecho este servidor es de la envoltura de seguridad, y esa envoltura debe ser probable, rapida y auditable sin el stack de vision por computadora presente. Las pruebas estan escritas como ataques: falsificar un token, repetirlo, confirmar por reflejo, correr contra la aprobacion del humano, escapar de la lista permitida por symlink. El adaptador se verifica aparte contra el motor real de extremo a extremo, incluyendo una aplicacion completa y su undo a traves de exiftool.

## Familia

[shutter-cull](https://github.com/keivanmalhani/shutter-cull) es el motor. [shutter-mcp](https://github.com/keivanmalhani/shutter-mcp) es su hermano de solo lectura, y sigue siendo de solo lectura a proposito. [shutter-select](https://github.com/keivanmalhani/shutter-select) hace lo mismo para video.

## Licencia

MIT, ver [LICENSE](LICENSE).
