"""Prueba rapida desde la terminal:
    python demo.py ranking "llama esferica de n-butano en microgravedad"
    python demo.py recomendar --archivo mis_datos.csv
    python demo.py afinidad "gota de n-heptano 0.5 mm 1 atm 21% O2"
    python demo.py chat "¿Que experimentos usan n-butano?"
"""
import argparse, json
from motor_psi import Motor

p = argparse.ArgumentParser()
p.add_argument("uso", choices=["ranking", "afinidad", "recomendar", "chat"])
p.add_argument("texto", nargs="?", default="")
p.add_argument("--archivo", action="append", default=[])
p.add_argument("-k", type=int, default=8)
a = p.parse_args()
m = Motor()
if a.uso == "ranking":
    r = m.ranking(a.texto, a.archivo, k=a.k)
    for x in r["resultados"]:
        print(f'{x["puntaje_rrf"]:.4f}  {x["tipo"]:<13} {x["id"]}')
elif a.uso == "afinidad":
    r = m.afinidad(a.texto, a.archivo)
    for x in r["investigaciones"]:
        print(f'{x["investigacion"]}: total={x["afinidad_total"]} ({x["nivel_total"]})  numerica={x["afinidad_numerica"]}  semantica={x["afinidad_semantica"]}')
        print("   ", "; ".join(x["sentido"][:3]))
elif a.uso == "recomendar":
    r = m.recomendar(a.texto, a.archivo)
    print("Fuente:", r["fuente"], "|", r.get("motivo", ""))
    print(json.dumps(r["texto"], indent=1, ensure_ascii=False))
else:
    r = m.chat(a.texto, a.archivo)
    print(r["respuesta"] or f'(sin respuesta: {r["fuente"]} - {r["motivo"]})')
    print("Fuentes:", [f["id"] for f in r["fuentes"]])
