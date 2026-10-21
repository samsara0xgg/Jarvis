"""The notch's trip cards: the departure pin (ADR 0203) and the followed bus ride (ADR 0224)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, model_validator

from jarvis.surface.inherent_server.calls import home_call

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


class DepartureRequest(BaseModel):
    """Body of ``POST /inherent/departure`` (ADR 0203, 0204): the field each action needs."""

    action: Literal["pin", "add", "remove", "next", "undo"]
    offer_id: str = Field(default="", max_length=40)
    index: int = Field(default=0, ge=0, le=9)
    pin_id: str = Field(default="", max_length=40)
    trip_id: str = Field(default="", max_length=60)


class RideRequest(BaseModel):
    """Body of ``POST /inherent/ride`` (ADR 0224): board or not on a question, or end the ride."""

    action: Literal["board", "not", "end"]
    candidate_id: str = Field(default="", max_length=40)
    ride_id: str = Field(default="", max_length=40)

    @model_validator(mode="after")
    def _needs_its_id(self) -> RideRequest:
        if self.action != "end" and not self.candidate_id:
            msg = "candidate_id is required to answer the question"
            raise ValueError(msg)
        return self


def register_departure_route(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0203: the notch's trip cards."""
    if deps.departure_act is None:
        return
    departure_act = deps.departure_act

    @app.post("/inherent/departure", status_code=200)
    async def departure(req: DepartureRequest) -> dict[str, Any]:
        """``{departure, reason}``: the pin now served, and why an action changed nothing."""
        return await home_call(departure_act(req.model_dump()))


def register_ride_routes(app: FastAPI, deps: InherentDeps) -> None:
    """ADR 0224: ``GET`` the ride now, ``POST`` his answer to the boarding question or its end."""
    if deps.ride_act is None or deps.ride_read is None:
        return
    ride_act, ride_read = deps.ride_act, deps.ride_read

    @app.get("/inherent/ride")
    async def ride_now() -> dict[str, Any]:
        """``{ride}``: the ride he is on with its progress, or ``null``."""
        return {"ride": await home_call(ride_read())}

    @app.post("/inherent/ride", status_code=200)
    async def ride(req: RideRequest) -> dict[str, Any]:
        """``{ride, reason}``: the ride now, and why the answer changed nothing (or ``null``)."""
        return await home_call(ride_act(req.model_dump()))
