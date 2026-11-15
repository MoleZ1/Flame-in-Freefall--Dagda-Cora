"""
motor_psi — embeddings, ranking y sistema de recomendacion sobre la base PSI (MongoDB).

Uso desde su aplicacion:
    from motor_psi import Motor
    m = Motor()                                   # lee MongoDB y el indice de embeddings
    m.ranking("llama esferica de n-butano", k=10)
    m.afinidad(texto="gota de n-heptano a 1 atm", archivos=["datos.csv"])
    m.recomendar(archivos=["datos.csv"])           # recomendaciones en texto (Qwen, verificadas)
    m.chat("¿Que experimentos usan n-butano?")
Configuracion por variables de entorno (ver config.env.ejemplo).
"""
from __future__ import annotations

import os

from .ingesta import leer_archivo
from .recomendador import recomendar as _recomendar
from .servicio import Servicio

__all__ = ["Motor"]


class Motor:
    def __init__(self):
        self.srv = Servicio()

    def _entradas(self, archivos):
        out = []
        for a in archivos or []:
            if isinstance(a, (tuple, list)):          # (nombre, bytes) p. ej. desde un formulario web
                nombre, contenido = a
            else:                                     # ruta en disco
                nombre, contenido = os.path.basename(a), open(a, "rb").read()
            out.append(leer_archivo(nombre, contenido, self.srv.b))
        return out

    def ranking(self, texto: str = "", archivos=None, k: int = 10, tipos=None, diversificar: bool = True) -> dict:
        """tipos: subconjunto de {"investigacion","dataset","experimento","fragmento","hallazgo"}"""
        return self.srv.ranking(texto, self._entradas(archivos), k=k, tipos=set(tipos) if tipos else None, diversificar=diversificar)

    def afinidad(self, texto: str = "", archivos=None) -> dict:
        return self.srv.afinidad(self._entradas(archivos), texto)

    def recomendar(self, texto: str = "", archivos=None) -> dict:
        return _recomendar(self.srv, self._entradas(archivos), texto)

    def chat(self, pregunta: str, archivos=None) -> dict:
        return self.srv.chat(pregunta, self._entradas(archivos))
