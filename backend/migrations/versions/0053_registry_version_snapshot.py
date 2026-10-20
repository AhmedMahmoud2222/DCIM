"""G5: append-only registry ledger; first persisted registry-v2 activation.

Sibling G6/G8 migrations must be reparented before integration; draft parent is
merged G4-R 0049. Frozen literals here intentionally never import runtime code.
"""

import sqlalchemy as sa
from alembic import op

revision = "0053_registry_version_snapshot"
down_revision = "0049_telemetry_replay_ledger"
branch_labels = None
depends_on = None

FROZEN_SNAPSHOTS = (
    ("1", """dcim.registry-snapshot.v1
version=1
precision=0.00000001
rounding=ROUND_HALF_EVEN
decimal_precision=28
unit.%.symbol=%
unit.%.dimension=ratio
unit.%.scale=0.01
unit.%.offset=0
unit.1.symbol=1
unit.1.dimension=ratio
unit.1.scale=1
unit.1.offset=0
unit.A.symbol=A
unit.A.dimension=current
unit.A.scale=1
unit.A.offset=0
unit.CFM.symbol=CFM
unit.CFM.dimension=volumetric_flow
unit.CFM.scale=0.0004719474432
unit.CFM.offset=0
unit.K.symbol=K
unit.K.dimension=temperature
unit.K.scale=1
unit.K.offset=-273.15
unit.L/s.symbol=L/s
unit.L/s.dimension=volumetric_flow
unit.L/s.scale=0.001
unit.L/s.offset=0
unit.Pa.symbol=Pa
unit.Pa.dimension=pressure
unit.Pa.scale=1
unit.Pa.offset=0
unit.V.symbol=V
unit.V.dimension=voltage
unit.V.scale=1
unit.V.offset=0
unit.W.symbol=W
unit.W.dimension=power
unit.W.scale=1
unit.W.offset=0
unit.cm.symbol=cm
unit.cm.dimension=length
unit.cm.scale=0.01
unit.cm.offset=0
unit.degC.symbol=degC
unit.degC.dimension=temperature
unit.degC.scale=1
unit.degC.offset=0
unit.degF.symbol=degF
unit.degF.dimension=temperature
unit.degF.scale=0.5555555555555555555555555556
unit.degF.offset=-32
unit.ft.symbol=ft
unit.ft.dimension=length
unit.ft.scale=0.3048
unit.ft.offset=0
unit.ft/min.symbol=ft/min
unit.ft/min.dimension=velocity
unit.ft/min.scale=0.00508
unit.ft/min.offset=0
unit.g.symbol=g
unit.g.dimension=mass
unit.g.scale=0.001
unit.g.offset=0
unit.in.symbol=in
unit.in.dimension=length
unit.in.scale=0.0254
unit.in.offset=0
unit.inH2O.symbol=inH2O
unit.inH2O.dimension=pressure
unit.inH2O.scale=249.08891
unit.inH2O.offset=0
unit.kPa.symbol=kPa
unit.kPa.dimension=pressure
unit.kPa.scale=1000
unit.kPa.offset=0
unit.kW.symbol=kW
unit.kW.dimension=power
unit.kW.scale=1000
unit.kW.offset=0
unit.kg.symbol=kg
unit.kg.dimension=mass
unit.kg.scale=1
unit.kg.offset=0
unit.km/h.symbol=km/h
unit.km/h.dimension=velocity
unit.km/h.scale=0.2777777777777777777777777778
unit.km/h.offset=0
unit.lb.symbol=lb
unit.lb.dimension=mass
unit.lb.scale=0.45359237
unit.lb.offset=0
unit.m.symbol=m
unit.m.dimension=length
unit.m.scale=1
unit.m.offset=0
unit.m/s.symbol=m/s
unit.m/s.dimension=velocity
unit.m/s.scale=1
unit.m/s.offset=0
unit.m3/h.symbol=m3/h
unit.m3/h.dimension=volumetric_flow
unit.m3/h.scale=0.0002777777777777777777777778
unit.m3/h.offset=0
unit.m3/s.symbol=m3/s
unit.m3/s.dimension=volumetric_flow
unit.m3/s.scale=1
unit.m3/s.offset=0
unit.mm.symbol=mm
unit.mm.dimension=length
unit.mm.scale=0.001
unit.mm.offset=0
alias.amp=A
alias.amps=A
alias.c=degC
alias.celsius=degC
alias.cfm=CFM
alias.f=degF
alias.fahrenheit=degF
alias.fpm=ft/min
alias.inh2o=inH2O
alias.inwc=inH2O
alias.kelvin=K
alias.kilowatt=kW
alias.kilowatts=kW
alias.l/s=L/s
alias.pascal=Pa
alias.pascals=Pa
alias.pct=%
alias.percent=%
alias.ratio=1
alias.volt=V
alias.volts=V
alias.watt=W
alias.watts=W
alias.°c=degC
alias.°f=degF
metric.airflow_m3_s.key=airflow_m3_s
metric.airflow_m3_s.dimension=volumetric_flow
metric.airflow_m3_s.canonical_unit=m3/s
metric.airflow_m3_s.presentation_unit=m3/h
metric.airflow_velocity_m_s.key=airflow_velocity_m_s
metric.airflow_velocity_m_s.dimension=velocity
metric.airflow_velocity_m_s.canonical_unit=m/s
metric.airflow_velocity_m_s.presentation_unit=m/s
metric.availability.key=availability
metric.availability.dimension=ratio
metric.availability.canonical_unit=1
metric.availability.presentation_unit=%
metric.cooling_output_kw.key=cooling_output_kw
metric.cooling_output_kw.dimension=power
metric.cooling_output_kw.canonical_unit=kW
metric.cooling_output_kw.presentation_unit=kW
metric.differential_pressure_pa.key=differential_pressure_pa
metric.differential_pressure_pa.dimension=pressure
metric.differential_pressure_pa.canonical_unit=Pa
metric.differential_pressure_pa.presentation_unit=Pa
metric.humidity_percent.key=humidity_percent
metric.humidity_percent.dimension=ratio
metric.humidity_percent.canonical_unit=%
metric.humidity_percent.presentation_unit=%
metric.load_percent.key=load_percent
metric.load_percent.dimension=ratio
metric.load_percent.canonical_unit=%
metric.load_percent.presentation_unit=%
metric.power_kw.key=power_kw
metric.power_kw.dimension=power
metric.power_kw.canonical_unit=kW
metric.power_kw.presentation_unit=kW
metric.return_air_temperature_c.key=return_air_temperature_c
metric.return_air_temperature_c.dimension=temperature
metric.return_air_temperature_c.canonical_unit=degC
metric.return_air_temperature_c.presentation_unit=degC
metric.supply_air_temperature_c.key=supply_air_temperature_c
metric.supply_air_temperature_c.dimension=temperature
metric.supply_air_temperature_c.canonical_unit=degC
metric.supply_air_temperature_c.presentation_unit=degC
metric.temperature_c.key=temperature_c
metric.temperature_c.dimension=temperature
metric.temperature_c.canonical_unit=degC
metric.temperature_c.presentation_unit=degC""",
     "d29e5d41a996e5b081583e87f8e6a270b4040b682432c16aaad7b0f22f9ac6d4"),
    ("2", """dcim.registry-snapshot.v1
version=2
precision=0.00000001
rounding=ROUND_HALF_EVEN
decimal_precision=28
unit.%.symbol=%
unit.%.dimension=ratio
unit.%.scale=0.01
unit.%.offset=0
unit.1.symbol=1
unit.1.dimension=ratio
unit.1.scale=1
unit.1.offset=0
unit.A.symbol=A
unit.A.dimension=current
unit.A.scale=1
unit.A.offset=0
unit.CFM.symbol=CFM
unit.CFM.dimension=volumetric_flow
unit.CFM.scale=0.0004719474432
unit.CFM.offset=0
unit.K.symbol=K
unit.K.dimension=temperature
unit.K.scale=1
unit.K.offset=-273.15
unit.L/s.symbol=L/s
unit.L/s.dimension=volumetric_flow
unit.L/s.scale=0.001
unit.L/s.offset=0
unit.Pa.symbol=Pa
unit.Pa.dimension=pressure
unit.Pa.scale=1
unit.Pa.offset=0
unit.V.symbol=V
unit.V.dimension=voltage
unit.V.scale=1
unit.V.offset=0
unit.W.symbol=W
unit.W.dimension=power
unit.W.scale=1
unit.W.offset=0
unit.cm.symbol=cm
unit.cm.dimension=length
unit.cm.scale=0.01
unit.cm.offset=0
unit.degC.symbol=degC
unit.degC.dimension=temperature
unit.degC.scale=1
unit.degC.offset=0
unit.degF.symbol=degF
unit.degF.dimension=temperature
unit.degF.scale=0.5555555555555555555555555556
unit.degF.offset=-32
unit.ft.symbol=ft
unit.ft.dimension=length
unit.ft.scale=0.3048
unit.ft.offset=0
unit.ft/min.symbol=ft/min
unit.ft/min.dimension=velocity
unit.ft/min.scale=0.00508
unit.ft/min.offset=0
unit.g.symbol=g
unit.g.dimension=mass
unit.g.scale=0.001
unit.g.offset=0
unit.in.symbol=in
unit.in.dimension=length
unit.in.scale=0.0254
unit.in.offset=0
unit.inH2O.symbol=inH2O
unit.inH2O.dimension=pressure
unit.inH2O.scale=249.08891
unit.inH2O.offset=0
unit.kPa.symbol=kPa
unit.kPa.dimension=pressure
unit.kPa.scale=1000
unit.kPa.offset=0
unit.kW.symbol=kW
unit.kW.dimension=power
unit.kW.scale=1000
unit.kW.offset=0
unit.kg.symbol=kg
unit.kg.dimension=mass
unit.kg.scale=1
unit.kg.offset=0
unit.km/h.symbol=km/h
unit.km/h.dimension=velocity
unit.km/h.scale=0.2777777777777777777777777778
unit.km/h.offset=0
unit.lb.symbol=lb
unit.lb.dimension=mass
unit.lb.scale=0.45359237
unit.lb.offset=0
unit.m.symbol=m
unit.m.dimension=length
unit.m.scale=1
unit.m.offset=0
unit.m/s.symbol=m/s
unit.m/s.dimension=velocity
unit.m/s.scale=1
unit.m/s.offset=0
unit.m3/h.symbol=m3/h
unit.m3/h.dimension=volumetric_flow
unit.m3/h.scale=0.0002777777777777777777777778
unit.m3/h.offset=0
unit.m3/s.symbol=m3/s
unit.m3/s.dimension=volumetric_flow
unit.m3/s.scale=1
unit.m3/s.offset=0
unit.mm.symbol=mm
unit.mm.dimension=length
unit.mm.scale=0.001
unit.mm.offset=0
alias.amp=A
alias.amps=A
alias.c=degC
alias.celsius=degC
alias.cfm=CFM
alias.f=degF
alias.fahrenheit=degF
alias.fpm=ft/min
alias.inh2o=inH2O
alias.inwc=inH2O
alias.kelvin=K
alias.kilowatt=kW
alias.kilowatts=kW
alias.l/s=L/s
alias.pascal=Pa
alias.pascals=Pa
alias.pct=%
alias.percent=%
alias.ratio=1
alias.volt=V
alias.volts=V
alias.watt=W
alias.watts=W
alias.°c=degC
alias.°f=degF
metric.airflow_m3_s.key=airflow_m3_s
metric.airflow_m3_s.dimension=volumetric_flow
metric.airflow_m3_s.canonical_unit=m3/s
metric.airflow_m3_s.presentation_unit=m3/h
metric.airflow_m3_s.value_kind=gauge
metric.airflow_m3_s.min_canonical=0
metric.airflow_m3_s.max_canonical=none
metric.airflow_m3_s.allowed_source_units=m3/s,m3/h,L/s,CFM
metric.airflow_m3_s.aggregations=avg,min,max,last
metric.airflow_m3_s.rule_types=threshold_high,threshold_low
metric.airflow_velocity_m_s.key=airflow_velocity_m_s
metric.airflow_velocity_m_s.dimension=velocity
metric.airflow_velocity_m_s.canonical_unit=m/s
metric.airflow_velocity_m_s.presentation_unit=m/s
metric.airflow_velocity_m_s.value_kind=gauge
metric.airflow_velocity_m_s.min_canonical=0
metric.airflow_velocity_m_s.max_canonical=none
metric.airflow_velocity_m_s.allowed_source_units=m/s,ft/min,km/h
metric.airflow_velocity_m_s.aggregations=avg,min,max,last
metric.airflow_velocity_m_s.rule_types=threshold_high,threshold_low
metric.availability.key=availability
metric.availability.dimension=ratio
metric.availability.canonical_unit=1
metric.availability.presentation_unit=%
metric.availability.value_kind=state
metric.availability.min_canonical=0
metric.availability.max_canonical=1
metric.availability.allowed_source_units=1,%
metric.availability.aggregations=avg,min,max,last
metric.availability.rule_types=availability_unavailable
metric.cooling_output_kw.key=cooling_output_kw
metric.cooling_output_kw.dimension=power
metric.cooling_output_kw.canonical_unit=kW
metric.cooling_output_kw.presentation_unit=kW
metric.cooling_output_kw.value_kind=gauge
metric.cooling_output_kw.min_canonical=0
metric.cooling_output_kw.max_canonical=none
metric.cooling_output_kw.allowed_source_units=W,kW
metric.cooling_output_kw.aggregations=avg,min,max,last
metric.cooling_output_kw.rule_types=threshold_high,threshold_low
metric.differential_pressure_pa.key=differential_pressure_pa
metric.differential_pressure_pa.dimension=pressure
metric.differential_pressure_pa.canonical_unit=Pa
metric.differential_pressure_pa.presentation_unit=Pa
metric.differential_pressure_pa.value_kind=gauge
metric.differential_pressure_pa.min_canonical=none
metric.differential_pressure_pa.max_canonical=none
metric.differential_pressure_pa.allowed_source_units=Pa,kPa,inH2O
metric.differential_pressure_pa.aggregations=avg,min,max,last
metric.differential_pressure_pa.rule_types=threshold_high,threshold_low
metric.humidity_percent.key=humidity_percent
metric.humidity_percent.dimension=ratio
metric.humidity_percent.canonical_unit=%
metric.humidity_percent.presentation_unit=%
metric.humidity_percent.value_kind=bounded_ratio
metric.humidity_percent.min_canonical=0
metric.humidity_percent.max_canonical=100
metric.humidity_percent.allowed_source_units=%,1
metric.humidity_percent.aggregations=avg,min,max,last
metric.humidity_percent.rule_types=threshold_high,threshold_low
metric.load_percent.key=load_percent
metric.load_percent.dimension=ratio
metric.load_percent.canonical_unit=%
metric.load_percent.presentation_unit=%
metric.load_percent.value_kind=gauge
metric.load_percent.min_canonical=0
metric.load_percent.max_canonical=none
metric.load_percent.allowed_source_units=%,1
metric.load_percent.aggregations=avg,min,max,last
metric.load_percent.rule_types=threshold_high,threshold_low
metric.power_kw.key=power_kw
metric.power_kw.dimension=power
metric.power_kw.canonical_unit=kW
metric.power_kw.presentation_unit=kW
metric.power_kw.value_kind=gauge
metric.power_kw.min_canonical=0
metric.power_kw.max_canonical=none
metric.power_kw.allowed_source_units=W,kW
metric.power_kw.aggregations=avg,min,max,last
metric.power_kw.rule_types=threshold_high,threshold_low
metric.return_air_temperature_c.key=return_air_temperature_c
metric.return_air_temperature_c.dimension=temperature
metric.return_air_temperature_c.canonical_unit=degC
metric.return_air_temperature_c.presentation_unit=degC
metric.return_air_temperature_c.value_kind=gauge
metric.return_air_temperature_c.min_canonical=-273.15
metric.return_air_temperature_c.max_canonical=none
metric.return_air_temperature_c.allowed_source_units=degC,degF,K
metric.return_air_temperature_c.aggregations=avg,min,max,last
metric.return_air_temperature_c.rule_types=threshold_high,threshold_low
metric.supply_air_temperature_c.key=supply_air_temperature_c
metric.supply_air_temperature_c.dimension=temperature
metric.supply_air_temperature_c.canonical_unit=degC
metric.supply_air_temperature_c.presentation_unit=degC
metric.supply_air_temperature_c.value_kind=gauge
metric.supply_air_temperature_c.min_canonical=-273.15
metric.supply_air_temperature_c.max_canonical=none
metric.supply_air_temperature_c.allowed_source_units=degC,degF,K
metric.supply_air_temperature_c.aggregations=avg,min,max,last
metric.supply_air_temperature_c.rule_types=threshold_high,threshold_low
metric.temperature_c.key=temperature_c
metric.temperature_c.dimension=temperature
metric.temperature_c.canonical_unit=degC
metric.temperature_c.presentation_unit=degC
metric.temperature_c.value_kind=gauge
metric.temperature_c.min_canonical=-273.15
metric.temperature_c.max_canonical=none
metric.temperature_c.allowed_source_units=degC,degF,K
metric.temperature_c.aggregations=avg,min,max,last
metric.temperature_c.rule_types=threshold_high,threshold_low""",
     "2f9bd2d613f32baed8322fc244d5f33d74437d0fb10f53bc047fff2ce8e8c147"),
)


def upgrade() -> None:
    op.create_table(
        "registry_version_snapshot",
        sa.Column("version", sa.Text(), primary_key=True),
        sa.Column("canonical_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_registry_version_snapshot_hash")),
    )
    for version, canonical_text, content_hash in FROZEN_SNAPSHOTS:
        op.get_bind().execute(sa.text(
            "INSERT INTO registry_version_snapshot(version, canonical_text, content_hash) "
            "VALUES (:version, :canonical_text, :content_hash)"
        ), {"version": version, "canonical_text": canonical_text, "content_hash": content_hash})
    op.execute("""CREATE FUNCTION forbid_registry_snapshot_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'registry snapshots are append-only'; END $$""")
    op.execute("""CREATE TRIGGER registry_snapshot_no_row_mutation
        BEFORE UPDATE OR DELETE ON registry_version_snapshot
        FOR EACH ROW EXECUTE FUNCTION forbid_registry_snapshot_mutation()""")
    op.execute("""CREATE TRIGGER registry_snapshot_no_truncate
        BEFORE TRUNCATE ON registry_version_snapshot
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_registry_snapshot_mutation()""")


def downgrade() -> None:
    # Explicit rollback is safe only before any v2 contract has been persisted.
    op.execute("LOCK TABLE integration_metric_mapping, integration_metric_mapping_revision, "
               "telemetry_reading, daily_telemetry_aggregate, alarm_rule, registry_version_snapshot IN ACCESS EXCLUSIVE MODE")
    bind = op.get_bind()
    for table in (
        "integration_metric_mapping", "integration_metric_mapping_revision", "telemetry_reading",
        "daily_telemetry_aggregate", "alarm_rule",
    ):
        if bind.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE registry_version = '2')")):
            raise RuntimeError("Cannot downgrade G5 while persisted registry-v2 contracts exist")
    op.drop_table("registry_version_snapshot")
    op.execute("DROP FUNCTION forbid_registry_snapshot_mutation()")
