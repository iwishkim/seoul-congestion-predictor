#!/usr/bin/env python3
"""
서울 혼잡도 예측 모델 학습 스크립트
====================================

수집 로그(CSV)를 읽어 (지역 × 요일유형 × 시간대)별 혼잡 패턴을 학습하고,
웹앱(index.html)에 내장할 모델을 data/model.json 으로 저장합니다.

모델 방식
---------
딥러닝이 아닌 '패턴 기반 통계 예측기'입니다. 데이터가 약 3주치로 크지 않아,
과적합 위험이 큰 복잡한 모델보다 (지역·요일·시간)별 과거 분포를 집계하는
방식이 더 안정적이고 해석 가능합니다. 각 조건별로:
  - 4개 혼잡 등급(여유/보통/약간 붐빔/붐빔)의 확률 분포
  - 평균 추정 인구
  - 표본 수(n)
를 계산해 둡니다. 앱은 이를 조회해 예측 등급·신뢰도·예상 인구를 보여줍니다.

요일 유형
---------
약 3주(20일)치 데이터로는 7개 요일이나 토/일을 시간대별로 분리하면 칸당
표본이 1~2개로 줄어 예측이 불안정합니다. 따라서 평일(wd) / 주말(we, 토+일)
2개 유형으로 학습해 안정성을 확보하고, 특정 칸이 비면 요일 무관 전체 평균
(all)으로 자동 보정(폴백)합니다. 토요일과 일요일의 거시적 차이는 README의
'한계' 항목에 별도로 기록해 둡니다.

사용법
------
    python scripts/train_model.py  [입력 CSV 경로]
기본 입력: data/seoul_population_log.csv
"""
import sys, json, os
import pandas as pd

LEVELS = ["여유", "보통", "약간 붐빔", "붐빔"]
LEVEL_IDX = {lv: i for i, lv in enumerate(LEVELS)}

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_CSV = os.path.join(ROOT, "data", "seoul_population_log.csv")


def daytype(dow: int) -> str:
    return "we" if dow >= 5 else "wd"


def round500(x: float) -> int:
    return int(round(x / 500.0) * 500)


def build(csv_path: str) -> dict:
    df = pd.read_csv(csv_path)
    df = df.dropna(subset=["혼잡도", "중앙추정인구"])
    df["수집시간"] = pd.to_datetime(df["수집시간"])
    df["hour"] = df["수집시간"].dt.hour
    df["dt"] = df["수집시간"].dt.dayofweek.map(daytype)

    # 지역 좌표(지도용). 없으면 해당 지역은 좌표 null 처리.
    coords_path = os.path.join(ROOT, "data", "area_coords.json")
    coords = {}
    if os.path.exists(coords_path):
        with open(coords_path, encoding="utf-8") as f:
            coords = json.load(f)

    areas = sorted(df["지역명"].unique())
    missing = [a for a in areas if a not in coords]
    if missing:
        print(f"[경고] 좌표 없는 지역 {len(missing)}개: {missing}")
    model = {}

    for area in areas:
        sub = df[df["지역명"] == area]
        base_pop = round500(sub["중앙추정인구"].mean())
        cells = {"wd": {}, "we": {}, "all": {}}

        def fill(group, store):
            for hour, gh in group.groupby("hour"):
                n = len(gh)
                counts = [0, 0, 0, 0]
                for lv, c in gh["혼잡도"].value_counts().items():
                    counts[LEVEL_IDX[lv]] = int(c)
                # 라플라스 평활: 표본이 적을수록 확률이 균등분포로 수축 →
                # n=1 같은 칸이 100% 확신처럼 보이지 않도록 신뢰도를 정직하게 보정
                denom = n + 4  # +1 per class
                probs = [round((counts[i] + 1) / denom * 100) for i in range(4)]
                probs[probs.index(max(probs))] += 100 - sum(probs)  # 합 100 보정
                store[int(hour)] = probs + [round500(gh["중앙추정인구"].mean()), n]

        fill(sub[sub["dt"] == "wd"], cells["wd"])
        fill(sub[sub["dt"] == "we"], cells["we"])
        fill(sub, cells["all"])  # 요일 무관 폴백
        entry = {"base_pop": base_pop, "cells": cells}
        if area in coords:
            entry["lat"], entry["lng"] = coords[area]
        model[area] = entry

    available_hours = sorted(int(h) for h in df["hour"].unique())
    meta = {
        "levels": LEVELS,
        "areas": areas,
        "available_hours": available_hours,
        "span": [str(df["수집시간"].min().date()), str(df["수집시간"].max().date())],
        "n_obs": int(len(df)),
        "n_areas": len(areas),
    }
    return {"meta": meta, "model": model}


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV
    out = build(csv_path)
    out_path = os.path.join(ROOT, "data", "model.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = os.path.getsize(out_path) / 1024
    m = out["meta"]
    print(f"학습 완료: {m['n_areas']}개 지역, {m['n_obs']:,}건 관측")
    print(f"기간 {m['span'][0]} ~ {m['span'][1]}, 가용 시간대 {m['available_hours']}")
    print(f"저장: {out_path} ({size_kb:.0f} KB)")

    # index.html 에 모델을 인라인으로 주입(있을 경우)
    html_path = os.path.join(ROOT, "index.html")
    if os.path.exists(html_path):
        with open(html_path, encoding="utf-8") as f:
            html = f.read()
        payload = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
        start = html.find("/*__MODEL_START__*/")
        end = html.find("/*__MODEL_END__*/")
        if start != -1 and end != -1:
            new = (html[: start + len("/*__MODEL_START__*/")]
                   + "\nwindow.__DATA__=" + payload + ";\n"
                   + html[end:])
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(new)
            print(f"index.html 에 모델 주입 완료")


if __name__ == "__main__":
    main()
