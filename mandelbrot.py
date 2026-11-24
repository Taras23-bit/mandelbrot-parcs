# -*- coding: utf-8 -*-
"""Паралельна побудова множини Мандельброта: послідовно та через PARCS.

Локальний послідовний запуск без сторонніх бібліотек:
    python mandelbrot.py --width 400 --height 300 --max-iter 200
    python mandelbrot.py --input input_sequential.json --output result.json
    python mandelbrot.py --input input_sequential.json --image mandelbrot.png

Для PARCS завантажте цей файл як Solution File, а JSON нижче як Input File:
    {"mode": "parallel", "width": 1600, "height": 1200, "max_iter": 400,
     "x_min": -2.2, "x_max": 1.0, "y_min": -1.2, "y_max": 1.2,
     "distribution": "cyclic"}
Для послідовного запуску змініть mode на "sequential".

Точка c = x + iy належить множині Мандельброта, якщо послідовність
z(0) = 0, z(n+1) = z(n)^2 + c не виходить за коло |z| <= 2. Для кожного
пікселя рахується кількість ітерацій до виходу (не більше max_iter).

Рядки зображення розподіляються між воркерами циклічно: воркер i отримує
рядки i, i + N, i + 2N, ... Так «важкі» рядки біля центру множини
рівномірно розходяться між воркерами. Режим "block" віддає кожному
воркеру суцільну смугу рядків (для порівняння балансування).

Для кожного рядка рахується SHA-256 від кількостей ітерацій. Підсумковий
image_sha256 — SHA-256 від хешів рядків у порядку рядків, тому він не
залежить від кількості воркерів і способу розподілу.

elapsed_seconds включає розсилання завдань воркерам, обчислення та
збирання відповідей, але не читання/запис файлів і запуск PARCS.

Синтаксис сумісний з Python 2.7 і Python 3.
"""

from __future__ import division, print_function

import argparse
import hashlib
import json
import struct
import sys
import time
import zlib

try:
    from Pyro4 import expose
    HAS_PYRO4 = True
except ImportError:
    HAS_PYRO4 = False

    def expose(method):
        return method

try:
    integer_types = (int, long)  # noqa: F821 - Python 2.7.
    iterate = xrange  # noqa: F821 - Python 2.7.
except NameError:
    integer_types = (int,)
    iterate = range

clock = getattr(time, "perf_counter", time.time)
CLOCK_NAME = "perf_counter" if hasattr(time, "perf_counter") else "time.time"

DEFAULTS = {"mode": "sequential", "width": 1600, "height": 1200, "max_iter": 400,
            "x_min": -2.2, "x_max": 1.0, "y_min": -1.2, "y_max": 1.2,
            "distribution": "cyclic"}


def validate_config(raw):
    """Перевіряємо JSON до початку обчислень."""
    if not isinstance(raw, dict):
        raise ValueError("Input must be a JSON object.")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ValueError("Unknown parameters: " + ", ".join(sorted(unknown)))
    config = dict(DEFAULTS)
    config.update(raw)
    if config["mode"] not in ("sequential", "parallel"):
        raise ValueError("mode must be 'sequential' or 'parallel'.")
    if config["distribution"] not in ("cyclic", "block"):
        raise ValueError("distribution must be 'cyclic' or 'block'.")
    for name in ("width", "height", "max_iter"):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, integer_types) or value < 1:
            raise ValueError(name + " must be a positive integer.")
    for name in ("x_min", "x_max", "y_min", "y_max"):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, integer_types + (float,)):
            raise ValueError(name + " must be a number.")
        config[name] = float(value)
    if config["x_min"] >= config["x_max"] or config["y_min"] >= config["y_max"]:
        raise ValueError("x_min < x_max and y_min < y_max are required.")
    return config


def compute_row(row, width, height, max_iter, x_min, x_max, y_min, y_max):
    """Кількість ітерацій для кожного пікселя одного рядка зображення."""
    ci = y_max - (y_max - y_min) * row / (height - 1 if height > 1 else 1)
    dx = (x_max - x_min) / (width - 1 if width > 1 else 1)
    counts = []
    for column in iterate(width):
        cr = x_min + dx * column
        zr = zi = 0.0
        n = 0
        while n < max_iter:
            zr2 = zr * zr
            zi2 = zi * zi
            if zr2 + zi2 > 4.0:
                break
            zi = 2.0 * zr * zi + ci
            zr = zr2 - zi2 + cr
            n += 1
        counts.append(n)
    return counts


def row_digest(counts):
    """SHA-256 рядка: кожна кількість ітерацій як 2 байти (big-endian)."""
    data = bytearray()
    for n in counts:
        data.append((n >> 8) & 0xFF)
        data.append(n & 0xFF)
    return hashlib.sha256(bytes(data)).hexdigest()


def process_rows(rows, width, height, max_iter, x_min, x_max, y_min, y_max):
    """Спільне ядро: рахує задані рядки, повертає їхні хеші й статистику.

    Повертає список трійок (номер рядка, SHA-256 рядка, сума ітерацій)
    і кількість точок, що належать множині (досягли max_iter).
    """
    results = []
    inside = 0
    for row in rows:
        counts = compute_row(row, width, height, max_iter, x_min, x_max, y_min, y_max)
        inside += sum(1 for n in counts if n == max_iter)
        results.append((row, row_digest(counts), sum(counts)))
    return results, inside


def assign_rows(height, parts, distribution):
    """Списки рядків для кожного воркера: циклічно або суцільними смугами."""
    parts = min(parts, height)
    if distribution == "cyclic":
        return [list(range(index, height, parts)) for index in iterate(parts)]
    return [list(range(height * index // parts, height * (index + 1) // parts))
            for index in iterate(parts)]


def image_args(config):
    return (config["width"], config["height"], config["max_iter"], config["x_min"],
            config["x_max"], config["y_min"], config["y_max"])


def run_sequential(config):
    """Усе зображення в поточному процесі."""
    results, inside = process_rows(range(config["height"]), *image_args(config))
    return results, inside, 0, [len(results)]


def run_parallel(config, workers):
    """Розсилаємо кожному воркеру його рядки, потім збираємо відповіді."""
    if not HAS_PYRO4:
        raise RuntimeError("Parallel mode requires Pyro4 in the PARCS environment.")
    if not workers:
        raise RuntimeError("Parallel mode requires connected PARCS workers.")

    assignment = assign_rows(config["height"], len(workers), config["distribution"])
    pending = []
    for index, rows in enumerate(assignment):
        pending.append(workers[index].process_part(rows, *image_args(config)))

    # Усі запити вже відправлені; тепер чекаємо відповіді кожного воркера.
    results = []
    inside = 0
    rows_per_worker = []
    for part in pending:
        part_results, part_inside = part.value
        results.extend(part_results)
        inside += part_inside
        rows_per_worker.append(len(part_results))
    return results, inside, len(assignment), rows_per_worker


def build_mandelbrot(raw_config, workers=None):
    """Будуємо зображення обраним режимом і вимірюємо час обчислень."""
    config = validate_config(raw_config)
    started = clock()
    if config["mode"] == "sequential":
        results, inside, workers_used, rows_per_worker = run_sequential(config)
    else:
        results, inside, workers_used, rows_per_worker = run_parallel(config, workers)
    elapsed = clock() - started

    results = sorted((tuple(item) for item in results), key=lambda item: item[0])
    if [item[0] for item in results] != list(range(config["height"])):
        raise RuntimeError("Invalid result: some rows are missing or duplicated.")
    image_digest = hashlib.sha256(
        "".join(item[1] for item in results).encode("ascii")).hexdigest()
    total_iterations = sum(item[2] for item in results)
    pixels = config["width"] * config["height"]
    return {
        "mode": config["mode"],
        "distribution": config["distribution"],
        "width": config["width"],
        "height": config["height"],
        "pixels": pixels,
        "max_iter": config["max_iter"],
        "region": [config["x_min"], config["x_max"], config["y_min"], config["y_max"]],
        "total_iterations": total_iterations,
        "inside_points": inside,
        "inside_percent": round(100.0 * inside / pixels, 3),
        "image_sha256": image_digest,
        "elapsed_seconds": elapsed,
        "workers_used": workers_used,
        "rows_per_worker": rows_per_worker,
        "timer": CLOCK_NAME,
    }


class Solver(object):
    """Контракт завантажуваної задачі Hummer12007/parcs-python."""

    def __init__(self, workers=None, input_file_name=None, output_file_name=None):
        self.workers = workers
        self.input_file_name = input_file_name
        self.output_file_name = output_file_name

    def solve(self):
        with open(self.input_file_name, "r") as source:
            config = json.load(source)
        result = build_mandelbrot(config, self.workers)
        text = json.dumps(result, indent=2, sort_keys=True)
        with open(self.output_file_name, "w") as destination:
            destination.write(text + "\n")
        print(text)
        return result

    @staticmethod
    @expose
    def process_part(rows, width, height, max_iter, x_min, x_max, y_min, y_max):
        """Віддалений метод, що виконується на worker."""
        return process_rows(rows, width, height, max_iter, x_min, x_max, y_min, y_max)


def color(n, max_iter):
    """Колір пікселя: точки множини чорні, решта — градієнт за ітераціями."""
    if n >= max_iter:
        return (12, 16, 32)
    t = (n / float(max_iter)) ** 0.35
    return (int(9 * (1 - t) * t ** 3 * 255), int(15 * (1 - t) ** 2 * t ** 2 * 255),
            int(8.5 * (1 - t) ** 3 * t * 255 + 40 * (1 - t)))


def write_png(path, config):
    """Зберегти зображення у PNG без сторонніх бібліотек (лише для звіту)."""
    width, height, max_iter = config["width"], config["height"], config["max_iter"]
    raw = bytearray()
    for row in iterate(height):
        raw.append(0)
        for n in compute_row(row, *image_args(config)):
            raw.extend(min(255, max(0, v)) for v in color(n, max_iter))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    with open(path, "wb") as destination:
        destination.write(b"\x89PNG\r\n\x1a\n")
        destination.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        destination.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        destination.write(chunk(b"IEND", b""))


def main():
    parser = argparse.ArgumentParser(description="Mandelbrot set / PARCS-Python")
    parser.add_argument("--input", help="Input JSON; otherwise use CLI parameters.")
    parser.add_argument("--output", help="Save result JSON to this path.")
    parser.add_argument("--image", help="Also save the picture as PNG.")
    parser.add_argument("--width", type=int, default=400)
    parser.add_argument("--height", type=int, default=300)
    parser.add_argument("--max-iter", type=int, default=200)
    args = parser.parse_args()

    try:
        if args.input:
            with open(args.input, "r") as source:
                config = json.load(source)
        else:
            config = {"width": args.width, "height": args.height, "max_iter": args.max_iter}
        if isinstance(config, dict) and config.get("mode") == "parallel":
            config = dict(config, mode="sequential")
            print("Note: the CLI runs the sequential version; use PARCS for parallel.",
                  file=sys.stderr)
        result = build_mandelbrot(config)
        text = json.dumps(result, indent=2, sort_keys=True)
        if args.output:
            with open(args.output, "w") as destination:
                destination.write(text + "\n")
        if args.image:
            write_png(args.image, validate_config(config))
        print(text)
        return 0
    except (ValueError, RuntimeError, IOError) as error:
        print("Error: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
