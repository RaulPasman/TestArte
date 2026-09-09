#!/usr/bin/env python3
"""
Baja una vez las fotos de las obras y las deja guardadas en el repositorio,
en la carpeta img/, junto con img-manifest.json (un array del mismo largo
que OBRAS, con la ruta local de cada foto o null si no se consiguió).

Corre server-side (GitHub Actions), no en el navegador del visitante:
por eso puede usar un User-Agent propio (Wikimedia lo pide) y no depende
de la red de quien abre la página.

Lee la lista de obras directamente del archivo HTML (regex sobre el
array OBRAS), así que si el día de mañana se agrega o saca una obra ahí,
alcanza con volver a correr este script — no hay una segunda lista que
mantener sincronizada a mano.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

CANDIDATOS_HTML = [
    "estilos-de-arte.html", "Estilos-de-arte.html",
    "duelo-de-cuadros.html", "Duelo-de-cuadros.html",
]
CARPETA_IMG = "img"
MANIFEST = "img-manifest.json"
ANCHO = 1200  # una sola resolución guardada; el navegador la achica si hace falta

W_API = "https://en.wikipedia.org/w/api.php"
C_API = "https://commons.wikimedia.org/w/api.php"

HEADERS = {
    "User-Agent": "ArteRaul-ImageFetcher/1.0 "
                  "(https://raulpasman.github.io/; uso personal, no comercial) "
                  "python-urllib"
}

NO_SIRVE = re.compile(
    r"(portrait of|photograph|photo of|signature|grave|tomb|plaque|memorial|"
    r"stamp|coin|logo|building|exterior|museum of|bust of|statue|"
    r"map of the|attends|at the opening|in his studio|in her studio|"
    r"press conference|red carpet|receiving|award|interview|"
    r"\.svg|\.pdf|\.ogv|\.ogg|\.webm|\.djvu)", re.I)
ES_IMG = re.compile(r"\.(jpe?g|png|tiff?|gif)$", re.I)


def es_probable_retrato(titulo, artista):
    """Heurística extra: un archivo cuyo nombre es casi solo el nombre del
    artista (más año, foto de agencia o número) suele ser una foto de la
    persona, no de la obra."""
    def normalizar(s):
        s = re.sub(r"^File:", "", s, flags=re.I)
        s = re.sub(r"\.[a-zA-Z]{2,4}$", "", s)
        s = re.sub(r"[\(\),.\-–—_]|\b(19|20)\d{2}\b|\bno\.?\s*\d+\b", " ", s, flags=re.I)
        return re.sub(r"\s+", " ", s).strip().lower()
    return normalizar(titulo) == normalizar(artista)


def encontrar_fuente():
    for c in CANDIDATOS_HTML:
        if os.path.exists(c):
            return c
    presentes = sorted(f for f in os.listdir(".") if f.lower().endswith(".html"))
    sys.exit(
        "No encontré ninguno de estos archivos en la raíz del repo: "
        + ", ".join(CANDIDATOS_HTML) + ".\n"
        + "Archivos .html que sí hay en la raíz: "
        + (", ".join(presentes) if presentes else "ninguno") + "."
    )


def leer_obras(path):
    txt = open(path, encoding="utf-8").read()
    m = re.search(r"OBRAS\s*=\s*\[(.*?)\n\];", txt, re.S)
    if not m:
        sys.exit("No encontré el array OBRAS en " + path)
    filas = re.findall(
        r'\{t:"(.*?)",ar:"(.*?)",y:(\d+),s:"(.*?)",w:"(.*?)"\}', m.group(1))
    obras = []
    for i, (t, ar, y, s, w) in enumerate(filas):
        obras.append({"i": i, "t": t, "ar": ar, "y": y, "s": s, "w": w})
    return obras


def es_solo_artista(o):
    def sin_acentos(s):
        import unicodedata
        s = unicodedata.normalize("NFD", s.lower())
        return "".join(c for c in s if unicodedata.category(c) != "Mn")
    w = sin_acentos(o["w"])
    w = re.sub(r"\([^)]*\)", " ", w)
    w = re.sub(r"[^a-z0-9\s-]", " ", w).strip()
    ar = sin_acentos(o["ar"])
    tk = [x for x in w.split() if len(x) >= 3]
    ult = tk[-1] if tk else ""
    return (len(w) > 2 and w in ar) or (len(tk) <= 2 and len(ult) >= 4 and ult in ar)


def pag_w(t):
    return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(t.replace(" ", "_"))


def pag_c(t):
    return "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(t.replace(" ", "_"))


def api(base, params, intentos=4):
    qs = dict(params)
    qs.update({"format": "json", "formatversion": "2", "origin": "*"})
    url = base + "?" + urllib.parse.urlencode(qs)
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            ultimo = e
            time.sleep(1.0 * (i + 1))
    print("  aviso: fallo de API tras reintentos:", ultimo)
    return {}


def por_titulo(lote):
    """Lote de obras (con w = título de la obra). Devuelve {indice: (img_url, page_url)}."""
    titulos = "|".join(o["w"] for o in lote)
    d = api(W_API, {"action": "query", "redirects": "1", "prop": "pageimages",
                     "piprop": "thumbnail", "pithumbsize": str(ANCHO),
                     "pilimit": "50", "titles": titulos})
    q = d.get("query", {})
    mapa, pags = {}, {}
    for x in q.get("normalized", []):
        mapa[x["from"]] = x["to"]
    for x in q.get("redirects", []):
        mapa[x["from"]] = x["to"]
    for p in q.get("pages", []):
        pags[p.get("title")] = p
    out = {}
    for o in lote:
        t, salto = o["w"], 0
        while t in mapa and salto < 6:
            t = mapa[t]
            salto += 1
        p = pags.get(t)
        if p and p.get("thumbnail", {}).get("source"):
            out[o["i"]] = (p["thumbnail"]["source"], pag_w(t))
    return out


def por_busqueda(o, evitar):
    q = o["t"] + " " + o["ar"] + " painting"
    d = api(W_API, {"action": "query", "generator": "search", "gsrsearch": q,
                     "gsrlimit": "5", "gsrnamespace": "0", "prop": "pageimages",
                     "piprop": "thumbnail", "pithumbsize": str(ANCHO), "pilimit": "10"})
    pags = sorted(d.get("query", {}).get("pages", []) or [],
                  key=lambda p: p.get("index", 99))
    for p in pags:
        th = p.get("thumbnail", {}).get("source")
        if th and th not in evitar:
            return th, pag_w(p.get("title", ""))
    return None, None


def por_commons(consulta, artista, evitar):
    d = api(C_API, {"action": "query", "generator": "search", "gsrsearch": consulta,
                     "gsrnamespace": "6", "gsrlimit": "14", "prop": "imageinfo",
                     "iiprop": "url", "iiurlwidth": str(ANCHO)})
    pags = sorted(d.get("query", {}).get("pages", []) or [],
                  key=lambda p: p.get("index", 99))
    for p in pags:
        titulo = p.get("title", "")
        if not ES_IMG.search(titulo) or NO_SIRVE.search(titulo):
            continue
        if es_probable_retrato(titulo, artista):
            continue
        ii = (p.get("imageinfo") or [None])[0]
        if ii:
            u = ii.get("thumburl") or ii.get("url")
            if u and u not in evitar:
                return u, pag_c(titulo)
    return None, None


def descargar(url, destino):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=40) as r, open(destino, "wb") as f:
        f.write(r.read())


def main():
    fuente = encontrar_fuente()
    obras = leer_obras(fuente)
    print(f"{len(obras)} obras leídas de {fuente}")
    os.makedirs(CARPETA_IMG, exist_ok=True)

    manifest = [None] * len(obras)
    ya = set()
    if os.path.exists(MANIFEST):
        try:
            previo = json.load(open(MANIFEST, encoding="utf-8"))
            for i, entrada in enumerate(previo):
                if entrada and entrada.get("img") and os.path.exists(entrada["img"]) and i < len(manifest):
                    manifest[i] = entrada
                    ya.add(i)
        except Exception:
            pass
    print(f"ya resueltas de una corrida anterior: {len(ya)}")

    candidatos = {}  # indice -> (img_url, page_url)

    # 1) lote grande por título de obra (rápido, pocas requests)
    pendientes = [o for o in obras if o["i"] not in ya and not es_solo_artista(o)]
    for k in range(0, len(pendientes), 40):
        lote = pendientes[k:k + 40]
        candidatos.update(por_titulo(lote))
        print(f"  lote {k}-{k+len(lote)}: {len(candidatos)} url candidatas hasta ahora")
        time.sleep(0.3)

    usadas = set(u for (u, _) in candidatos.values())

    # 2) búsqueda individual para lo que no salió en el lote
    for o in obras:
        if o["i"] in ya or o["i"] in candidatos or es_solo_artista(o):
            continue
        img_url, page_url = por_busqueda(o, usadas)
        if img_url:
            candidatos[o["i"]] = (img_url, page_url)
            usadas.add(img_url)
        time.sleep(0.25)

    # 3) Commons por artista — SOLO para obras marcadas explícitamente como
    #    "solo artista" (es_solo_artista). Una obra con título puntual que no
    #    se pudo resolver en los pasos 1 y 2 queda sin foto antes que arriesgarse
    #    a traer un retrato del artista en vez de la obra.
    for o in obras:
        if o["i"] in ya or o["i"] in candidatos or not es_solo_artista(o):
            continue
        img_url, page_url = por_commons(o["ar"] + " " + o["s"], o["ar"], usadas)
        if not img_url:
            img_url, page_url = por_commons(o["ar"], o["ar"], usadas)
        if img_url:
            candidatos[o["i"]] = (img_url, page_url)
            usadas.add(img_url)
        time.sleep(0.25)

    print(f"urls candidatas totales: {len(candidatos)} de {len(obras) - len(ya)} pendientes")

    ok, fallos = 0, 0
    for i, (img_url, page_url) in candidatos.items():
        destino = os.path.join(CARPETA_IMG, f"{i:03d}.jpg")
        try:
            descargar(img_url, destino)
            manifest[i] = {"img": destino.replace(os.sep, "/"), "url": page_url}
            ok += 1
        except Exception as e:
            print(f"  fallo descarga obra {i}: {e}")
            fallos += 1
        time.sleep(0.15)

    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)

    total_con_foto = sum(1 for x in manifest if x)
    print(f"Listo: {total_con_foto} de {len(obras)} obras con foto guardada "
          f"({ok} nuevas, {fallos} fallidas esta corrida, {len(ya)} ya venían de antes).")


if __name__ == "__main__":
    main()
