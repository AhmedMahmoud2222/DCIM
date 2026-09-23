"""Idempotently seed a private, simulated topology for local evaluation only."""

import asyncio

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.domain.identity.models import ManagedAsset
from app.domain.network.models import NetworkConnection, NetworkDevice, NetworkInterface
from app.domain.physical.models import Equipment


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(NetworkDevice.id).limit(1))
        if existing:
            print("Network demo data already exists; no changes made.")
            return
        devices: list[NetworkDevice] = []
        for tag, name, device_type in (
            ("DEMO-NET-CORE-01", "Core-SW-01", "core_switch"),
            ("DEMO-NET-ACCESS-A1", "Access-SW-A1", "access_switch"),
        ):
            asset = ManagedAsset(asset_type="network_device", asset_tag=tag, lifecycle_status="active")
            db.add(asset)
            await db.flush()
            device = NetworkDevice(id=asset.id, name=name, device_type=device_type, source="demo")
            db.add(device)
            devices.append(device)
        equipment = (await db.execute(select(Equipment).order_by(Equipment.id).limit(3))).scalars().all()
        for item in equipment:
            device = NetworkDevice(id=item.id, name=item.hostname or str(item.id), device_type="endpoint", source="demo")
            db.add(device)
            devices.append(device)
        await db.flush()
        core_port = NetworkInterface(
            device_id=devices[0].id,
            name="Gi1/1/1",
            role="uplink",
            admin_status="up",
            oper_status="up",
            speed_mbps=10000,
            source="demo",
        )
        access_up = NetworkInterface(
            device_id=devices[1].id,
            name="Gi1/0/1",
            role="uplink",
            admin_status="up",
            oper_status="up",
            speed_mbps=10000,
            source="demo",
        )
        db.add_all([core_port, access_up])
        await db.flush()
        db.add(NetworkConnection(interface_a_id=access_up.id, interface_b_id=core_port.id, source="demo", is_authoritative=False))
        for index, endpoint in enumerate(devices[2:], start=20):
            nic = NetworkInterface(
                device_id=endpoint.id,
                name="eth0",
                role="server",
                admin_status="up",
                oper_status="up",
                speed_mbps=1000,
                native_vlan=100,
                source="demo",
            )
            switch_port = NetworkInterface(
                device_id=devices[1].id,
                name=f"Gi1/0/{index}",
                role="server",
                admin_status="up",
                oper_status="up",
                speed_mbps=1000,
                native_vlan=100,
                source="demo",
            )
            db.add_all([nic, switch_port])
            await db.flush()
            db.add(NetworkConnection(interface_a_id=nic.id, interface_b_id=switch_port.id, source="demo", is_authoritative=False))
        await db.commit()
        print(f"Seeded {len(devices)} simulated network devices without external contact.")


if __name__ == "__main__":
    asyncio.run(seed())
