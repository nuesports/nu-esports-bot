from utils import images


def test_slugify_strips_spaces_and_punctuation():
    assert images.slugify("D.Va") == "DVa"


def test_slugify_folds_accents_to_ascii():
    assert images.slugify("Torbjörn") == "Torbjorn"


def test_slugify_keeps_digits():
    assert images.slugify("76 (Soldier)") == "76Soldier"
