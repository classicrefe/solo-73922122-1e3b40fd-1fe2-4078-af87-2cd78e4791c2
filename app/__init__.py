"""Flask web application for pump cumulative-failure-incidence accounting."""
from __future__ import annotations

import os
import re

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, url_for)

from . import services
from .csvio import parse_csv
from .db import connect, init_db
from .lifetable import EVENT_LABELS, EVENT_TYPES, PumpRecord
from .meterreview import parse_review_csv, solve_review

_M_RE = re.compile(r"[0-9]{1,9}")


def _decode_upload(raw: bytes) -> str | None:
    """Decode an uploaded CSV as UTF-8 (with BOM) or GBK; None if neither."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            return None


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB upload cap
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
    app.config["DB_PATH"] = db_path

    def db_conn():
        from flask import g
        if "db" not in g:
            g.db = connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def _close_db(_exc):
        from flask import g
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.route("/health")
    def health():
        return jsonify(status="ok")

    # ------------------------------------------------------------- dashboard
    @app.route("/")
    def index():
        conn = db_conn()
        meta = services.get_meta(conn)
        table = services.get_life_table(conn)
        return render_template("index.html", meta=meta, table=table,
                               event_labels=EVENT_LABELS)

    # -------------------------------------------------------------- CSV upload
    @app.route("/upload", methods=["POST"])
    def upload():
        file = request.files.get("csv_file")
        if file is None or file.filename == "":
            flash("请先选择要上传的 CSV 文件", "error")
            return redirect(url_for("index"))
        raw = file.read()
        text = _decode_upload(raw)
        if text is None:
            flash("CSV 编码无法识别，请保存为 UTF-8 后重试", "error")
            return redirect(url_for("index"))
        try:
            records = parse_csv(text)
            result = services.replace_ledger(db_conn(), records,
                                             source=f"csv:{file.filename}")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        except Exception as exc:  # pragma: no cover - defensive
            flash(f"写入失败，已保留原有台账与汇总: {exc}", "error")
            return redirect(url_for("index"))
        flash(f"上传成功：台账已整体替换为 {result['pump_count']} 台泵，汇总已同步重算",
              "ok")
        return redirect(url_for("index"))

    # ------------------------------------------------------------- pump ledger
    @app.route("/pumps")
    def pumps():
        rows = services.list_pumps(db_conn())
        return render_template("pumps.html", pumps=rows,
                               event_labels=EVENT_LABELS)

    @app.route("/pumps/new", methods=["GET", "POST"])
    def pump_new():
        return _pump_form(None)

    @app.route("/pumps/<pump_id>/edit", methods=["GET", "POST"])
    def pump_edit(pump_id: str):
        row = services.get_pump(db_conn(), pump_id)
        if row is None:
            abort(404)
        return _pump_form(row)

    def _pump_form(existing):
        conn = db_conn()
        if request.method == "GET":
            return render_template(
                "pump_form.html",
                existing=existing,
                event_types=EVENT_TYPES,
                event_labels=EVENT_LABELS,
                form=dict(existing) if existing else {},
                errors=[],
                review=None,
            )
        form, record, errors = _read_pump_form(request.form,
                                               fixed_id=existing["pump_id"] if existing else None)
        if errors:
            return render_template(
                "pump_form.html", existing=existing, event_types=EVENT_TYPES,
                event_labels=EVENT_LABELS, form=form, errors=errors,
                review=None,
            ), 400
        try:
            result = services.upsert_pump(conn, record)
        except ValueError as exc:
            return render_template(
                "pump_form.html", existing=existing, event_types=EVENT_TYPES,
                event_labels=EVENT_LABELS, form=form, errors=[str(exc)],
                review=None,
            ), 400
        except Exception as exc:  # pragma: no cover - defensive
            flash(f"保存失败，已保留原值: {exc}", "error")
            return redirect(url_for("pumps"))
        label = "更正" if result["action"] == "updated" else "新增"
        flash(f"泵 {record.pump_id} {label}成功，台账与汇总已在同一事务内生效", "ok")
        return redirect(url_for("pumps"))

    def _read_pump_form(form, fixed_id=None):
        errors = []
        pump_id = (fixed_id or form.get("pump_id", "")).strip()
        if not pump_id:
            errors.append("泵号不能为空")
        try:
            entry = float(form.get("entry_hours", ""))
            if entry < 0:
                errors.append("入册小时数不能为负")
        except ValueError:
            entry = None
            errors.append("入册小时数必须是数字")
        try:
            exit_hours = float(form.get("exit_hours", ""))
        except ValueError:
            exit_hours = None
            errors.append("出册小时数必须是数字")
        event_type = form.get("event_type", "")
        if event_type not in EVENT_TYPES:
            errors.append("结束方式必须是 故障 / 预防更换 / 观察结束 之一")
        if entry is not None and exit_hours is not None and exit_hours < entry:
            errors.append(f"出册小时数 ({exit_hours}) 不能早于入册小时数 ({entry})")
        record = None
        if not errors:
            record = PumpRecord(pump_id, entry, exit_hours, event_type)
        return dict(form), record, errors

    # ------------------------------------------------------- meter review
    @app.route("/pumps/review", methods=["POST"])
    def pump_review():
        """抄表复核：M + 抄表 CSV → 入册/出册各自的最小、最大工时。"""
        conn = db_conn()
        pump_id = request.form.get("pump_id", "").strip()
        m_raw = request.form.get("m", "").strip()
        file = request.files.get("review_csv")
        errors: list[str] = []

        if not pump_id:
            errors.append("泵号不能为空")
        m = None
        if not _M_RE.fullmatch(m_raw):
            errors.append("归零周期 M 须为至多 9 位的正整数")
        else:
            m = int(m_raw)
            if m == 0:
                errors.append("归零周期 M 须大于零")

        rows = None
        if file is None or file.filename == "":
            errors.append("请上传按抄表顺序排列的抄表 CSV")
        else:
            text = _decode_upload(file.read())
            if text is None:
                errors.append("抄表 CSV 编码无法识别，请保存为 UTF-8 后重试")
            else:
                try:
                    rows = parse_review_csv(text)
                except ValueError as exc:
                    errors.append(str(exc))

        result = None
        token = None
        if not errors and m is not None:
            result = solve_review(m, rows)
            if result.feasible and result.unique:
                try:
                    token = services.save_review_session(
                        conn, pump_id, m, result.entry_min, result.exit_min)
                except ValueError as exc:  # pragma: no cover - defensive
                    errors.append(str(exc))
                    result = None

        existing = services.get_pump(conn, pump_id) if pump_id else None
        review_ctx = {
            "pump_id": pump_id,
            "m_raw": m_raw,
            "filename": file.filename if file else "",
            "errors": errors,
            "result": result,
            "token": token,
        }
        status = 400 if errors else 200
        return render_template(
            "pump_form.html", existing=existing, event_types=EVENT_TYPES,
            event_labels=EVENT_LABELS,
            form=dict(existing) if existing else {"pump_id": pump_id},
            errors=[], review=review_ctx,
        ), status

    @app.route("/pumps/review/confirm", methods=["POST"])
    def pump_review_confirm():
        """确认采用复核工时：与汇总同一事务生效；失效/失败保留原值。"""
        token = request.form.get("token", "").strip()
        event_type = request.form.get("event_type", "")
        try:
            result = services.confirm_review_session(db_conn(), token, event_type)
        except services.ReviewStale as exc:
            flash(f"复核已失效，未写入任何数据：{exc}", "error")
            return redirect(url_for("pumps"))
        except ValueError as exc:
            flash(f"确认失败，已保留原值: {exc}", "error")
            return redirect(url_for("pumps"))
        except Exception as exc:  # pragma: no cover - defensive
            flash(f"确认失败，已保留原值: {exc}", "error")
            return redirect(url_for("pumps"))
        label = "更正" if result["action"] == "updated" else "新增"
        flash(f"泵 {result['pump_id']} 已按复核结果{label}：入册 "
              f"{result['entry_hours']:.0f} 小时、出册 {result['exit_hours']:.0f} 小时，"
              f"台账与汇总已在同一事务内生效", "ok")
        return redirect(url_for("pumps"))

    # ---------------------------------------------------- read-only query page
    @app.route("/query", methods=["GET"])
    def query_page():
        hours_raw = request.args.get("hours", "").strip()
        if hours_raw == "":
            return render_template("query.html", result=None, hours_raw="",
                                   error=None)
        try:
            hours = float(hours_raw)
            if hours < 0:
                raise ValueError
        except ValueError:
            return render_template(
                "query.html", result=None, hours_raw=hours_raw,
                error="请输入非负数字小时数",
            ), 400
        result = services.query_incidence(db_conn(), hours)
        return render_template("query.html", result=result,
                               hours_raw=hours_raw, error=None,
                               event_labels=EVENT_LABELS)

    @app.route("/api/query")
    def api_query():
        hours_raw = request.args.get("hours", "").strip()
        try:
            hours = float(hours_raw)
        except (TypeError, ValueError):
            return jsonify(error="参数 hours 必须是数字"), 400
        if hours < 0:
            return jsonify(error="参数 hours 不能为负"), 400
        return jsonify(services.query_incidence(db_conn(), hours))

    @app.route("/api/lifetable")
    def api_lifetable():
        return jsonify([dict(r) for r in services.get_life_table(db_conn())])

    with app.app_context():
        init_db(db_conn())

    return app
