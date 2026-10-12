-- Overwatch added Emerald between Platinum and Diamond. rank_value is the tier's
-- index * 5 divisions, so every stored rank from Diamond up moves one tier (5) to
-- keep decoding to the same tier. Labels are plain text and don't change.

UPDATE profile_role_ranks SET rank_value = rank_value + 5
WHERE game = 'overwatch' AND rank_value >= 20;

UPDATE profile_stats SET rank_value = rank_value + 5
WHERE game = 'overwatch' AND rank_value >= 20;
