"""Authoritative network devices, interfaces, and physical connections."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_network_topology"
down_revision = "0014_telemetry_asset_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("asset_type_allowed", "managed_asset", type_="check")
    op.create_check_constraint(
        "asset_type_allowed",
        "managed_asset",
        "asset_type IN ('rack','equipment','pdu','ups','generator','power_panel','sensor','cable','network_device')",
    )
    op.create_table(
        "network_device",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("device_type", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("last_observed_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "device_type IN ('core_switch','distribution_switch','access_switch','router','firewall','appliance','endpoint')",
            name="device_type_allowed",
        ),
        sa.CheckConstraint("source IN ('operator','import','collector','demo')", name="source_allowed"),
        sa.ForeignKeyConstraint(["id"], ["managed_asset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "network_interface",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("interface_type", sa.String(24), nullable=False),
        sa.Column("description", sa.String(512)),
        sa.Column("mac_address", sa.String(17)),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("admin_status", sa.String(16), nullable=False),
        sa.Column("oper_status", sa.String(16), nullable=False),
        sa.Column("speed_mbps", sa.Integer()),
        sa.Column("duplex", sa.String(16)),
        sa.Column("mtu", sa.Integer()),
        sa.Column("native_vlan", sa.Integer()),
        sa.Column("ip_address", postgresql.INET()),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("last_observed_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("interface_type IN ('physical','logical','management','port_channel')", name="interface_type_allowed"),
        sa.CheckConstraint("role IN ('management','data','uplink','server','unknown')", name="role_allowed"),
        sa.CheckConstraint("admin_status IN ('up','down','testing','unknown')", name="admin_status_allowed"),
        sa.CheckConstraint("oper_status IN ('up','down','testing','unknown')", name="oper_status_allowed"),
        sa.CheckConstraint("speed_mbps IS NULL OR speed_mbps > 0", name="speed_positive"),
        sa.CheckConstraint("mtu IS NULL OR mtu > 0", name="mtu_positive"),
        sa.CheckConstraint("native_vlan IS NULL OR native_vlan BETWEEN 1 AND 4094", name="native_vlan_range"),
        sa.CheckConstraint("source IN ('operator','import','collector','demo')", name="source_allowed"),
        sa.ForeignKeyConstraint(["device_id"], ["network_device.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id", "name", name="uq_network_interface_device_name"),
    )
    op.create_index("ix_network_interface_device_id", "network_interface", ["device_id"])
    op.create_table(
        "network_connection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("interface_a_id", sa.Uuid(), nullable=False),
        sa.Column("interface_b_id", sa.Uuid(), nullable=False),
        sa.Column("cable_label", sa.String(128)),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("is_authoritative", sa.Boolean(), nullable=False),
        sa.Column("last_observed_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("interface_a_id <> interface_b_id", name="no_self_connection"),
        sa.CheckConstraint("source IN ('operator','import','collector','demo')", name="source_allowed"),
        sa.ForeignKeyConstraint(["interface_a_id"], ["network_interface.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["interface_b_id"], ["network_interface.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("interface_a_id", name="uq_network_connection_interface_a"),
        sa.UniqueConstraint("interface_b_id", name="uq_network_connection_interface_b"),
    )
    op.create_index("ix_network_connection_endpoints", "network_connection", ["interface_a_id", "interface_b_id"])
    op.execute(
        """CREATE FUNCTION enforce_network_port_single_connection() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM network_connection WHERE id <> NEW.id AND
            (interface_a_id IN (NEW.interface_a_id, NEW.interface_b_id) OR
             interface_b_id IN (NEW.interface_a_id, NEW.interface_b_id))) THEN
            RAISE EXCEPTION 'network interface already connected';
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER trg_network_port_single_connection BEFORE INSERT OR UPDATE ON network_connection
        FOR EACH ROW EXECUTE FUNCTION enforce_network_port_single_connection();"""
    )
    op.execute(
        """INSERT INTO permission (id, resource, action, description) VALUES
        (gen_random_uuid(), 'network', 'read', 'read on network'),
        (gen_random_uuid(), 'network', 'manage', 'manage network')
        ON CONFLICT (resource, action) DO NOTHING"""
    )
    op.execute(
        """INSERT INTO role_permission (role_id, permission_id)
        SELECT r.id, p.id FROM role r CROSS JOIN permission p WHERE p.resource='network' AND
        ((r.name IN ('Administrator','DCIM Manager','Engineer') AND p.action IN ('read','manage')) OR
         (r.name IN ('Operator','Viewer') AND p.action='read')) ON CONFLICT DO NOTHING"""
    )


def downgrade() -> None:
    op.execute(
        """DELETE FROM role_permission USING permission
        WHERE role_permission.permission_id=permission.id AND permission.resource='network'"""
    )
    op.execute("DELETE FROM permission WHERE resource='network'")
    op.execute(
        """DROP TRIGGER trg_network_port_single_connection ON network_connection;
        DROP FUNCTION enforce_network_port_single_connection()"""
    )
    op.drop_index("ix_network_connection_endpoints", table_name="network_connection")
    op.drop_table("network_connection")
    op.drop_index("ix_network_interface_device_id", table_name="network_interface")
    op.drop_table("network_interface")
    op.drop_table("network_device")
    op.execute("DELETE FROM managed_asset WHERE asset_type='network_device'")
    op.drop_constraint("asset_type_allowed", "managed_asset", type_="check")
    op.create_check_constraint(
        "asset_type_allowed",
        "managed_asset",
        "asset_type IN ('rack','equipment','pdu','ups','generator','power_panel','sensor','cable')",
    )
