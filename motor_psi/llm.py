"""
Cliente unico para Qwen3.5-9B, sin importar como se sirva.

LLM_BACKEND=openai  (por defecto)  -> POST {QWEN_URL}/chat/completions   (vLLM, llama.cpp, Ollama /v1, API externa)
LLM_BACKEND=ollama                 -> POST {QWEN_URL}/api/chat            (API nativa de Ollama; permite "think": false)

Variables: QWEN_URL, QWEN_MODEL, QWEN_API_KEY, QWEN_TIMEOUT (s), QWEN_NO_THINK ("1" por defecto).

Varios servidores en orden de preferencia (p. ej. laptop con GPU primero, CPU de la VM como respaldo):
    QWEN_URL=http://localhost:11435,http://localhost:11434
    QWEN_MODEL=qwen3.5:9b,qwen3.5:4b
Si el primero no responde (apagado, sin tunel, timeout), se intenta el siguiente.
"""
from __future__ import annotations

import os
import re

import requests


def configurado() -> bool:
    return bool(os.environ.get("QWEN_URL"))


ultimo_servidor = None


def servidores() -> list[tuple[str, str]]:
    urls = [u.strip().rstrip("/") for u in os.environ.get("QWEN_URL", "").split(",") if u.strip()]
    modelos = [m.strip() for m in os.environ.get("QWEN_MODEL", "qwen3.5:9b").split(",") if m.strip()]
    return [(u, modelos[min(i, len(modelos) - 1)]) for i, u in enumerate(urls)]


# Muestreo recomendado por Qwen para el modo SIN razonamiento (tareas generales).
# Una temperatura muy baja (casi "greedy") provoca repeticiones en Qwen3.5.
MUESTREO = {
    "temperature": float(os.environ.get("QWEN_TEMPERATURA", 0.7)),
    "top_p": float(os.environ.get("QWEN_TOP_P", 0.8)),
    "top_k": int(os.environ.get("QWEN_TOP_K", 20)),
    "min_p": 0.0,
    "presence_penalty": float(os.environ.get("QWEN_PRESENCE_PENALTY", 1.5)),
}


def quitar_repeticiones(texto: str) -> str:
    """Red de seguridad: elimina parrafos y oraciones repetidos (algunas versiones de Ollama
    ignoran presence_penalty con Qwen3.5)."""
    vistos, parrafos = set(), []
    for p in re.split(r"\n\s*\n", texto.strip()):
        oraciones, salida = re.split(r"(?<=[.!?])\s+", p.strip()), []
        for o in oraciones:
            clave = re.sub(r"\W+", " ", o.lower()).strip()
            if clave and clave in vistos:
                continue
            if clave:
                vistos.add(clave)
            salida.append(o)
        if salida:
            parrafos.append(" ".join(salida))
    return "\n\n".join(parrafos)


def generar(sistema: str, usuario: str, max_tokens: int = 700, temperatura: float | None = None,
            esquema: dict | None = None) -> str:
    """esquema: JSON Schema opcional. Si se da, el servidor obliga a Qwen a responder con ese JSON
    (Ollama: "format"; vLLM / llama.cpp: "response_format")."""
    global ultimo_servidor
    errores = []
    for base, modelo in servidores():
        try:
            texto = _generar_en(base, modelo, sistema, usuario, max_tokens, temperatura, esquema)
            ultimo_servidor = {"url": base, "modelo": modelo}
            return texto
        except Exception as e:
            errores.append(f"{base}: {type(e).__name__}")
    raise ConnectionError("ningun servidor de Qwen respondio: " + "; ".join(errores))


def _generar_en(base, modelo, sistema, usuario, max_tokens, temperatura, esquema=None) -> str:
    m = dict(MUESTREO)
    if temperatura is not None:
        m["temperature"] = temperatura
    sin_pensar = os.environ.get("QWEN_NO_THINK", "1") == "1"
    timeout = float(os.environ.get("QWEN_TIMEOUT", 120))
    cab = {"Authorization": f"Bearer {os.environ['QWEN_API_KEY']}"} if os.environ.get("QWEN_API_KEY") else {}
    mensajes = [{"role": "system", "content": sistema}, {"role": "user", "content": usuario}]

    if os.environ.get("LLM_BACKEND", "openai") == "ollama":
        cuerpo = {"model": modelo, "messages": mensajes, "stream": False,
                  "options": {**m, "repeat_penalty": 1.0, "num_predict": max_tokens}}
        if sin_pensar:
            cuerpo["think"] = False
        if esquema:
            cuerpo["format"] = esquema
        r = requests.post(base + "/api/chat", json=cuerpo, headers=cab, timeout=timeout)
        r.raise_for_status()
        texto = r.json()["message"]["content"] or ""
    else:
        cuerpo = {"model": modelo, "messages": mensajes, "max_tokens": max_tokens, **m}   # top_k/min_p: vLLM y llama.cpp los aceptan
        if sin_pensar:
            cuerpo["chat_template_kwargs"] = {"enable_thinking": False}
        if esquema:
            cuerpo["response_format"] = {"type": "json_schema", "json_schema": {"name": "respuesta", "schema": esquema}}
        r = requests.post(base + "/chat/completions", json=cuerpo, headers=cab, timeout=timeout)
        r.raise_for_status()
        texto = r.json()["choices"][0]["message"].get("content") or ""
    texto = re.sub(r"<think>.*?</think>", "", texto, flags=re.S).strip()
    return texto if esquema else quitar_repeticiones(texto)
