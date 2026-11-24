# Паралельна побудова множини Мандельброта у PARCS

Лабораторна робота 1 з дисципліни «Хмарні обчислення», група ТК-42.
Реалізація на [PARCS Python](https://github.com/Hummer12007/parcs-python).

## Файли

| Файл | Призначення |
|---|---|
| `mandelbrot.py` | Solution File для PARCS + CLI для послідовного запуску й PNG |
| `input_sequential.json` | Input File, послідовний режим |
| `input_parallel.json` | Input File, паралельний режим, циклічний розподіл рядків |
| `input_parallel_block.json` | Input File, паралельний режим, розподіл суцільними смугами |
| `make_tables.py` | Збирає результати прогонів у таблиці для звіту |
| `mandelbrot.png` | Зображення, побудоване з тими самими параметрами |

## Алгоритм

Точка c = x + iy належить множині Мандельброта, якщо послідовність z₀ = 0, zₙ₊₁ = zₙ² + c не виходить за коло |z| ≤ 2.
Для кожного пікселя зображення 1600 × 1200 рахується кількість ітерацій до виходу, але не більше `max_iter` = 1000.

Рядки розподіляються між воркерами **циклічно**: воркер i отримує рядки i, i + N, i + 2N, …
Біля центру множини рядки «важкі» (багато точок доходять до 1000 ітерацій), а по краях «легкі».
Циклічний розподіл дає кожному з 4 воркерів рівно 25 % ітерацій.
Суцільні смуги (`block`) дали б середнім воркерам по 46 %, і прискорення не перевищило б ~2,2.

Для кожного рядка рахується SHA-256 від кількостей ітерацій.
`image_sha256` — SHA-256 від хешів рядків у порядку рядків, тому він однаковий для будь-якої кількості воркерів.
Очікуване значення: `c3737d53d31bdc9e6386291142f540bd00b4120998c76ef057e9a3517c965138`.

Швидко перевірити:
```
python mandelbrot.py --width 400 --height 300 --max-iter 200
python mandelbrot.py --input input_sequential.json --image mandelbrot.png
```

## Запуск у Google Cloud

Кожен вузол (master і кожен воркер) — окрема VM на Container-Optimized OS. Контейнер
`hummer12007/parcs-node` запускається startup-скриптом (`create-with-container` для нових
проєктів більше не підтримується).
```
gcloud config set compute/zone europe-north1-a
gcloud services enable compute.googleapis.com
gcloud compute firewall-rules create parcs-web --network=default --direction=INGRESS --action=ALLOW --rules=tcp:8080 --source-ranges=ВАШ_IPv4/32

cat > master.sh <<'EOS'
#!/bin/bash
iptables -I INPUT -j ACCEPT
docker start parcs 2>/dev/null || docker run -d --name parcs --restart always --network host -e PARCS_ARGS="master" hummer12007/parcs-node
EOS
gcloud compute instances create master --machine-type=e2-standard-2 --image-family=cos-stable --image-project=cos-cloud --metadata-from-file=startup-script=master.sh

MASTER_IP=$(gcloud compute instances describe master --format='get(networkInterfaces[0].networkIP)')
cat > worker.sh <<EOS
#!/bin/bash
iptables -I INPUT -j ACCEPT
docker start parcs 2>/dev/null || docker run -d --name parcs --restart always --network host -e PARCS_ARGS="worker $MASTER_IP" hummer12007/parcs-node
EOS
gcloud compute instances create worker1 worker2 worker3 worker4 --machine-type=e2-standard-2 --image-family=cos-stable --image-project=cos-cloud --metadata-from-file=startup-script=worker.sh
gcloud compute instances list
```
`iptables -I INPUT -j ACCEPT` відкриває вбудований фаєрвол COS для портів PARCS; доступ ззовні
все одно обмежує правило `parcs-web`.

Вебінтерфейс: `http://EXTERNAL_IP_MASTER:8080`. Після експериментів:
`gcloud compute instances stop master worker1 worker2 worker3 worker4`.

## Експеримент

1. Послідовний запуск: Solution File = `mandelbrot.py`, Input File = `input_sequential.json`.
   Для послідовної версії PARCS однаково потребує хоча б одного підключеного воркера.
2. Паралельний запуск: Input File = `input_parallel.json`, з 1, 2 і 4 увімкненими воркерами.
   Зайвих воркерів вимикайте у вебінтерфейсі: Nodes → Disable.
3. Додатково: `input_parallel_block.json` на 4 воркерах, щоб показати вплив балансування.
4. Кожну конфігурацію запустіть 3 рази. Output кожного прогону збережіть у `results/`
   як `seq_1.txt … seq_3.txt`, `w1_…`, `w2_…`, `w4_…`, `b4_…`.
5. Запустіть `python make_tables.py` і перенесіть числа у звіт.

Якщо послідовна версія працює значно довше 2–3 хв або коротше 30 с, змініть `max_iter` в усіх JSON.
