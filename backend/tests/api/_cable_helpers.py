import uuid

CABLES = "/api/v1/cables"


def cable_body(a: str, b: str, **extra) -> dict:
    return {
        "label": f"C-{uuid.uuid4().hex[:6]}", "cable_type": "copper_utp", "endpoint_a_port_id": a, "endpoint_b_port_id": b,
        **extra,
    }


async def create_cable(client, headers, a: str, b: str, **extra) -> dict:
    response = await client.post(CABLES, json=cable_body(a, b, **extra), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def if_match(version: int) -> dict:
    return {"If-Match": str(version)}
