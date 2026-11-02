-- Slots someone has asked to reserve every week, and what happened to each week's run.
-- A run's row is what stops the heads up going out twice, and what the skip button
-- flips, so both have to survive a restart. Cancelling a slot drops its runs too.

CREATE TABLE IF NOT EXISTS weekly_reservations (
    id                SERIAL PRIMARY KEY,
    owner_id          BIGINT NOT NULL,
    manager           VARCHAR(100) NOT NULL,
    team              VARCHAR(50) NOT NULL,
    num_pcs           INT NOT NULL,
    res_type          TEXT NOT NULL,
    include_back_room BOOLEAN NOT NULL,
    weekday           SMALLINT NOT NULL,  -- Monday is 0, like datetime.weekday()
    start_time        TIME NOT NULL,      -- Central wall clock, so it survives DST
    end_time          TIME NOT NULL,
    first_date        DATE NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS weekly_reservation_runs (
    weekly_id      INT NOT NULL REFERENCES weekly_reservations (id) ON DELETE CASCADE,
    run_date       DATE NOT NULL,
    status         TEXT NOT NULL,  -- notified, skipped, booking, booked, failed or missed
    confirm_at     TIMESTAMPTZ,
    reservation_id INT REFERENCES reservations (id) ON DELETE SET NULL,
    PRIMARY KEY (weekly_id, run_date)
);
