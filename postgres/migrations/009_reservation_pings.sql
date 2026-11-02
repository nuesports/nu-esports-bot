-- Which staff ping belongs to which reservation, so the reminder before it starts
-- survives a restart. Bookings can be days out, far longer than the bot stays up.
-- Cancelling the reservation drops its row, which is what stops the reminder.

CREATE TABLE IF NOT EXISTS reservation_pings (
    message_id     BIGINT PRIMARY KEY,
    channel_id     BIGINT NOT NULL,
    staff_id       BIGINT NOT NULL,
    reservation_id INT NOT NULL REFERENCES reservations (id) ON DELETE CASCADE,
    team           TEXT NOT NULL,
    -- what the ping says staff should book, so Edit booking moves it
    start_time     TIMESTAMPTZ NOT NULL,
    posted_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reminded       BOOLEAN NOT NULL DEFAULT FALSE
);
