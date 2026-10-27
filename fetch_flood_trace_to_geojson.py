#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""행정안전부 침수흔적도 OpenAPI (DSSP-IF-00117) 시도 단위 GeoJSON 일괄 추출 스크립트

- API 엔드포인트: https://www.safetydata.go.kr/V2/api/DSSP-IF-00117
- 데이터 상세 페이지: https://www.safetydata.go.kr/disaster-data/view?dataSn=108
"""

from collections import defaultdict
import json
import os
import re
import time
from typing import Any, Dict, List, Optional
import urllib.parse
import requests
from requests.adapters import HTTPAdapter
import urllib3
from urllib3.util.retry import Retry

# SSL 인증서 경고 무시 (재난안전데이터공유플랫폼 기본 샘플코드 권장 설정)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==============================================================================
# 1. 사용자 설정 변수 (서비스키 및 시도 단위 추출 옵션)
# ==============================================================================
# GitHub Secrets(SAFETYDATA_API_KEY)에 등록된 값을 우선 불러오며, 없을 경우 기본 변수값을 사용합니다.
SERVICE_KEY = os.environ.get("SAFETYDATA_API_KEY", "96PK8ATSGPMFJK0Q")

# 추출할 법정동 시도코드(2자리) 목록
# - 빈 리스트([])로 두면 전체 시도를 시도별 파일(data/flood_trace_시도코드_시도명.geojson)로 각각 나누어 저장합니다.
# - 특정 시도만 추출하려면 예: ["11", "41"] (서울특별시, 경기도) 형태로 지정합니다.
TARGET_CTPV_CODES: List[str] = []

# 1회 요청당 데이터 건수 및 최대 수집 페이지 수 (전체 데이터 1회 일괄 수집용)
NUM_OF_ROWS = 1000
MAX_PAGES = 1000  # 전체 건수(totalCount) 도달 시 자동 종료됨
OUTPUT_DIR = "data"

# ==============================================================================
# 2. API 상수 및 법정동 시도코드 매핑 테이블
# ==============================================================================
API_URL = "https://www.safetydata.go.kr/V2/api/DSSP-IF-00117"

# 법정동 시도코드(STDG_CTPV_CD) -> 시도명 매핑 (구 코드 및 특례/특별자치도 신규 코드 모두 포함)
CTPV_CODE_MAP: Dict[str, str] = {
    "11": "서울특별시",
    "26": "부산광역시",
    "27": "대구광역시",
    "28": "인천광역시",
    "29": "광주광역시",
    "30": "대전광역시",
    "31": "울산광역시",
    "36": "세종특별자치시",
    "41": "경기도",
    "42": "강원특별자치도",  # 구 강원도 코드(42) 데이터도 강원특별자치도로 통합 매핑
    "51": "강원특별자치도",
    "43": "충청북도",
    "44": "충청남도",
    "45": "전북특별자치도",  # 구 전라북도 코드(45) 데이터도 전북특별자치도로 통합 매핑
    "52": "전북특별자치도",
    "46": "전라남도",
    "47": "경상북도",
    "48": "경상남도",
    "50": "제주특별자치도",
}

# 출력결과(Response Element) 표준 속성 필드 (GEOM 제외한 13개 속성)
ATTRIBUTE_FIELDS = [
    "SN",  # 일련번호
    "FLDN_DOWA",  # 침수수심
    "FLDN_GRD",  # 침수등급
    "FLDN_AREA",  # 침수면적
    "FLDN_YR",  # 침수연도
    "FLDN_DST_NM",  # 침수재해명
    "FLDN_CS_DTL_NM",  # 침수원인상세명
    "FLDN_BGNG_YMD",  # 침수시작일자
    "FLDN_END_YMD",  # 침수종료일자
    "FLDN_BGNG_TM",  # 침수시작시각
    "FLDN_END_TM",  # 침수종료시각
    "STDG_CTPV_CD",  # 법정동시도코드 (2자리)
    "STDG_SGG_CD",  # 법정동시군구코드 (5자리)
]


def _parse_coord_ring(ring_text: str) -> List[List[float]]:
  """WKT 좌표 문자열('x1 y1, x2 y2, ...')을 [[x1, y1], [x2, y2], ...] 리스트로 변환합니다."""
  coords = []
  for pair in ring_text.strip().split(","):
    parts = pair.strip().split()
    if len(parts) >= 2:
      coords.append([float(parts[0]), float(parts[1])])
  return coords


def wkt_to_geojson_geometry(wkt_str: str) -> Optional[Dict[str, Any]]:
  """GEOM 필드의 WKT 문자열(POLYGON, MULTIPOLYGON 등)을 표준 GeoJSON geometry 객체로 변환합니다."""
  if not wkt_str or not isinstance(wkt_str, str):
    return None

  text = wkt_str.strip()
  if ";" in text and text.upper().startswith("SRID="):
    text = text.split(";", 1)[1].strip()

  if text.startswith("{"):
    try:
      return json.loads(text)
    except json.JSONDecodeError:
      pass

  try:
    from shapely import wkt
    from shapely.geometry import mapping

    geom = wkt.loads(text)
    return mapping(geom)
  except Exception:
    pass

  upper = text.upper()
  if upper.startswith("MULTIPOLYGON"):
    body = text[text.find("(") :].strip()
    polygons = []
    if body.startswith("(") and body.endswith(")"):
      inner = body[1:-1].strip()
      poly_blocks = re.split(r"\)\s*\)\s*,\s*\(\s*\(", inner)
      for block in poly_blocks:
        clean_block = block.strip().lstrip("(").rstrip(")")
        ring_strs = re.split(r"\)\s*,\s*\(", clean_block)
        rings = [_parse_coord_ring(r) for r in ring_strs if r.strip()]
        if rings:
          polygons.append(rings)
    return {"type": "MultiPolygon", "coordinates": polygons}

  elif upper.startswith("POLYGON"):
    body = text[text.find("(") :].strip()
    if body.startswith("(") and body.endswith(")"):
      inner = body[1:-1].strip().lstrip("(").rstrip(")")
      ring_strs = re.split(r"\)\s*,\s*\(", inner)
      rings = [_parse_coord_ring(r) for r in ring_strs if r.strip()]
      return {"type": "Polygon", "coordinates": rings}

  elif upper.startswith("LINESTRING"):
    body = text[text.find("(") :].strip().lstrip("(").rstrip(")")
    return {"type": "LineString", "coordinates": _parse_coord_ring(body)}

  elif upper.startswith("POINT"):
    body = text[text.find("(") :].strip().lstrip("(").rstrip(")")
    coords = _parse_coord_ring(body)
    if coords:
      return {"type": "Point", "coordinates": coords[0]}

  return None


def detect_crs_urn(sample_geom: Dict[str, Any]) -> str:
  """첫 번째 지오메트리의 좌표 범위를 확인하여 좌표계(CRS) URN을 자동 판별합니다."""
  coords = sample_geom.get("coordinates", [])
  while (
      isinstance(coords, (list, tuple))
      and len(coords) > 0
      and isinstance(coords[0], (list, tuple))
  ):
    coords = coords[0]

  if isinstance(coords, (list, tuple)) and len(coords) >= 2:
    x, y = float(coords[0]), float(coords[1])
    if 120.0 <= x <= 135.0 and 30.0 <= y <= 45.0:
      return "urn:ogc:def:crs:OGC:1.3:CRS84"
    elif 700000.0 <= x <= 1400000.0 and 1300000.0 <= y <= 2300000.0:
      return "urn:ogc:def:crs:EPSG::5179"
    elif 100000.0 <= x <= 400000.0 and 300000.0 <= y <= 700000.0:
      return "urn:ogc:def:crs:EPSG::5186"
  return "urn:ogc:def:crs:OGC:1.3:CRS84"


def extract_items_from_response(
    resp_json: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], int]:
  """재난안전데이터공유플랫폼 V2 API의 JSON 응답에서 데이터 배열(body)과 전체 건수(totalCount)를 추출합니다."""
  header = resp_json.get("header") or resp_json.get("response", {}).get(
      "header", {}
  )
  result_code = str(header.get("resultCode", "00"))
  if result_code not in ("00", "0", "200", "NORMAL"):
    result_msg = (
        header.get("resultMsg") or header.get("errorMsg") or "Unknown API Error"
    )
    raise RuntimeError(f"API 응답 오류 (코드 {result_code}): {result_msg}")

  body = resp_json.get("body")
  total_count = resp_json.get("totalCount", 0)

  if body is None and "response" in resp_json:
    resp_body = resp_json["response"].get("body", {})
    total_count = resp_body.get("totalCount", total_count)
    items = resp_body.get("items", resp_body)
    if isinstance(items, dict) and "item" in items:
      body = items["item"]
    elif isinstance(items, list):
      body = items

  if isinstance(body, dict):
    body = [body]
  elif not isinstance(body, list):
    body = []

  return body, int(total_count or len(body))


def normalize_ctpv_code(row: Dict[str, Any]) -> str:
  """STDG_CTPV_CD(2자리) 또는 STDG_SGG_CD(앞 2자리)에서 시도코드를 추출하고 구 코드를 표준화합니다."""
  ctpv_cd = str(row.get("STDG_CTPV_CD") or "").strip()
  if not ctpv_cd:
    sgg_cd = str(row.get("STDG_SGG_CD") or "").strip()
    if len(sgg_cd) >= 2:
      ctpv_cd = sgg_cd[:2]
  # 구 강원도(42)->51, 구 전라북도(45)->52 통합(파일 분리 일관성 유지)
  if ctpv_cd == "42":
    return "51"
  if ctpv_cd == "45":
    return "52"
  return ctpv_cd if ctpv_cd else "99"


def fetch_and_group_by_sido(
    service_key: str,
    num_of_rows: int = NUM_OF_ROWS,
    max_pages: int = MAX_PAGES,
    target_ctpv_codes: Optional[List[str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
  """API를 호출하여 시도코드(STDG_CTPV_CD)별로 GeoJSON Feature 리스트를 그룹화합니다."""
  decoded_key = (
      urllib.parse.unquote(service_key) if "%" in service_key else service_key
  )

  # 구/신규 코드 모두 매칭되도록 필터 세트 구성
  target_set = set()
  if target_ctpv_codes:
    for code in target_ctpv_codes:
      c = str(code).strip()
      target_set.add(c)
      if c in ("42", "51"):
        target_set.update(["42", "51"])
      if c in ("45", "52"):
        target_set.update(["45", "52"])

  # 네트워크 일시 오류 대비 자동 재시도 세션 설정
  session = requests.Session()
  retries = Retry(
      total=3, backoff_factor=1.0, status_forcelist=[500, 502, 503, 504]
  )
  session.mount("https://", HTTPAdapter(max_retries=retries))

  grouped_features: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

  for page_no in range(1, max_pages + 1):
    params = {
        "serviceKey": decoded_key,
        "returnType": "json",
        "pageNo": str(page_no),
        "numOfRows": str(num_of_rows),
    }

    print(f"[요청 중] pageNo={page_no}, numOfRows={num_of_rows} ...")
    response = session.get(API_URL, params=params, verify=False, timeout=60)

    content_text = response.text.strip()
    if content_text.startswith("<"):
      raise RuntimeError(
          "게이트웨이 XML 오류 응답이 반환되었습니다. 서비스키 등록 여부 또는 활용신청 승인 상태를 확인하세요:\n"
          f"{content_text[:500]}"
      )

    resp_json = response.json()
    items, total_count = extract_items_from_response(resp_json)

    if not items:
      print(
          f"[완료] {page_no}페이지에 더 이상 데이터가 없어 수집을 종료합니다."
      )
      break

    for row in items:
      raw_ctpv = str(row.get("STDG_CTPV_CD") or "").strip()
      if target_set and raw_ctpv not in target_set:
        continue

      geom_raw = row.get("GEOM")
      geometry = wkt_to_geojson_geometry(geom_raw)
      if not geometry:
        continue

      properties = {field: row.get(field) for field in ATTRIBUTE_FIELDS}
      for num_field in ("SN", "FLDN_DOWA", "FLDN_GRD", "FLDN_AREA"):
        val = properties.get(num_field)
        if val is not None and str(val).strip() != "":
          try:
            properties[num_field] = (
                float(val) if "." in str(val) else int(float(val))
            )
          except ValueError:
            pass

      norm_code = normalize_ctpv_code(row)
      grouped_features[norm_code].append({
          "type": "Feature",
          "geometry": geometry,
          "properties": properties,
      })

    total_collected = sum(len(v) for v in grouped_features.values())
    print(
        f"  -> {len(items)}건 수신 완료 (누적 유효 폴리곤: {total_collected}건"
        f" / 전체 데이터: {total_count}건)"
    )

    if page_no * num_of_rows >= total_count:
      print("[완료] 전체 페이지 수집이 끝났습니다.")
      break

    time.sleep(0.2)

  return grouped_features


def save_sido_geojson_files(
    grouped_features: Dict[str, List[Dict[str, Any]]],
    output_dir: str = OUTPUT_DIR,
) -> List[str]:
  """시도별로 분류된 Feature 리스트를 각각 개별 GeoJSON 파일로 저장합니다."""
  os.makedirs(output_dir, exist_ok=True)
  saved_files: List[str] = []

  for ctpv_cd, features in sorted(grouped_features.items()):
    if not features:
      continue

    sido_name = CTPV_CODE_MAP.get(ctpv_cd, f"기타_{ctpv_cd}")
    filename = f"flood_trace_{ctpv_cd}_{sido_name}.geojson"
    output_path = os.path.join(output_dir, filename)

    geojson_obj: Dict[str, Any] = {
        "type": "FeatureCollection",
        "name": f"MOIS_Flood_Trace_{ctpv_cd}_{sido_name}",
        "features": features,
    }

    if features[0].get("geometry"):
      crs_urn = detect_crs_urn(features[0]["geometry"])
      geojson_obj["crs"] = {"type": "name", "properties": {"name": crs_urn}}

    # GitHub 용량 절감 및 QGIS 로딩 속도 향상을 위해 불필요한 공백 없이 저장
    with open(output_path, "w", encoding="utf-8") as f:
      json.dump(geojson_obj, f, ensure_ascii=False, separators=(",", ":"))

    print(
        f"[저장 완료] {output_path} ({sido_name}: 총 {len(features)}개 폴리곤)"
    )
    saved_files.append(output_path)

  return saved_files


def main():
  if not SERVICE_KEY or SERVICE_KEY.strip() == "":
    raise SystemExit("오류: SERVICE_KEY 변수에 유효한 서비스키를 설정해주세요.")

  # 환경변수 TARGET_CTPV_CD가 지정되어 있으면(예: "11,41") 우선 적용
  env_targets = os.environ.get("TARGET_CTPV_CD", "").strip()
  target_codes = (
      [c.strip() for c in env_targets.split(",") if c.strip()]
      if env_targets
      else TARGET_CTPV_CODES
  )

  grouped = fetch_and_group_by_sido(
      service_key=SERVICE_KEY,
      num_of_rows=NUM_OF_ROWS,
      max_pages=MAX_PAGES,
      target_ctpv_codes=target_codes,
  )
  save_sido_geojson_files(grouped, output_dir=OUTPUT_DIR)


if __name__ == "__main__":
  main()
