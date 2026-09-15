"""Category assignment, genre blacklisting and destination folder naming."""
from marquee import catalog


def categorised(machines, catlist, config, reporter=None):
    screenless = catalog.derive_screenless(machines)
    kept = catalog.filter_machines(machines, screenless, config)
    return catalog.categorize(kept, catlist, config, reporter)


class TestCategories:
    def test_strips_the_arcade_prefix(self, machines, catlist, config):
        assert categorised(machines, catlist, config)["goodgame"]["category"] == "Maze / Misc."

    def test_keeps_the_mature_marker_for_the_folder_step(self, machines, catlist, config):
        assert categorised(machines, catlist, config)["maturegame"]["category"] == \
            "Casino / Misc. * Mature *"

    def test_machine_missing_from_catlist_is_unlisted(self, machines, catlist, config):
        assert categorised(machines, catlist, config)["nocat"]["category"] == "Unlisted"

    def test_every_survivor_gets_a_category(self, machines, catlist, config):
        result = categorised(machines, catlist, config)
        assert all("category" in info for info in result.values())


class TestGenreBlacklist:
    def test_blacklisted_genre_is_marked_not_dropped(self, machines, catlist, config):
        """Keeping it measurable is what lets a caller show what excluding it saves."""
        result = categorised(machines, catlist, config)
        assert result["boardgame1"]["excluded"] is True
        assert result["boardgame1"]["genre"] == "Board Game"

    def test_other_genres_survive(self, machines, catlist, config):
        result = categorised(machines, catlist, config)
        assert result["goodgame"]["excluded"] is False

    def test_every_machine_gets_a_genre(self, machines, catlist, config):
        result = categorised(machines, catlist, config)
        assert all("genre" in info for info in result.values())
        assert result["nocat"]["genre"] == "Unlisted"

    def test_unlisted_can_be_excluded_like_any_genre(self, machines, catlist, config):
        """Machines the catlist says nothing about used to be impossible to leave out."""
        config.blacklist_genres = ["Unlisted"]
        result = categorised(machines, catlist, config)
        assert result["nocat"]["excluded"] is True
        assert result["goodgame"]["excluded"] is False

    def test_unknown_genre_warns_instead_of_crashing(self, machines, catlist, config, reporter):
        config.blacklist_genres = ["Board Game", "No Such Genre"]
        result = categorised(machines, catlist, config, reporter)
        assert "No Such Genre" in reporter.text
        assert result["boardgame1"]["excluded"] is True

    def test_unknown_genres_share_one_warning_line(self, machines, catlist, config, reporter):
        """A trimmed catlist can miss a dozen; one line each would drown the output."""
        config.blacklist_genres = ["Nope One", "Nope Two", "Nope Three"]
        categorised(machines, catlist, config, reporter)
        assert len(reporter.warnings) == 1
        assert "3 blacklisted genre(s)" in reporter.warnings[0]

    def test_empty_blacklist_keeps_everything(self, machines, catlist, config):
        config.blacklist_genres = []
        assert "boardgame1" in categorised(machines, catlist, config)


class TestFolderNames:
    def test_slash_becomes_a_path_separator(self, config):
        assert catalog.folder_name("Maze / Misc.") == ("Maze/Misc", False)

    def test_dots_are_dropped(self, config):
        assert catalog.folder_name("Fighter / 2.5D") == ("Fighter/25D", False)

    def test_mature_goes_under_the_configured_folder(self, config):
        assert catalog.folder_name("Casino / Misc. * Mature *") == \
            ("ZZ-Adult/Casino/Misc", True)

    def test_mature_and_normal_agree_on_dots(self, config):
        """The two branches used to disagree: one trimmed dots, the other removed them."""
        plain, _ = catalog.folder_name("Casino / Misc.")
        mature, _ = catalog.folder_name("Casino / Misc. * Mature *")
        assert mature == f"ZZ-Adult/{plain}"

    def test_empty_segments_are_dropped(self, config):
        assert catalog.folder_name("Maze /  / Misc.") == ("Maze/Misc", False)

    def test_single_segment(self, config):
        assert catalog.folder_name("Unlisted") == ("Unlisted", False)


class TestSlashInsideAGenre:
    """A genre can contain a slash of its own, which only the spaced separator survives."""

    def test_genre_keeping_its_own_slash(self):
        from marquee import sources
        assert sources.genre_of("Game Console/Computer / Home Videogame/Home System") == \
            "Game Console/Computer"

    def test_subcategory_keeping_its_own_slash(self):
        from marquee import sources
        assert sources.genre_of("Arcade: Videocassette Player/Recorder / Misc.") == \
            "Videocassette Player/Recorder"

    def test_folder_name_keeps_the_two_real_levels(self, config):
        assert catalog.folder_name(
            "Game Console/Computer / Home Videogame/Home System") == \
            ("Game Console-Computer/Home Videogame-Home System", False)

    def test_folder_name_never_emits_a_bare_slash_segment(self, config):
        path, _ = catalog.folder_name("Videocassette Player/Recorder / Misc.")
        assert path == "Videocassette Player-Recorder/Misc"
        assert len(path.split("/")) == 2


class TestUnlistedIsBlacklistable:
    """`Unlisted` is this tool's own invention, not a catlist section.

    It still has to behave like any other genre -- one of its members, `dlair`, is
    11.5 GB on its own -- and it must not be reported as a typo.
    """

    def test_blacklisting_unlisted_marks_its_machines(self, catlist, config):
        config.blacklist_genres = [catalog.UNLISTED]
        result = catalog.categorize({"nocat": {"description": "No Category"}},
                                    catlist, config)
        assert result["nocat"]["genre"] == catalog.UNLISTED
        assert result["nocat"]["excluded"] is True

    def test_it_is_not_warned_about_as_unknown(self, catlist, config, reporter):
        config.blacklist_genres = [catalog.UNLISTED]
        catalog.categorize({"nocat": {"description": "No Category"}}, catlist, config,
                           reporter)
        assert not any("not in this catlist" in text for text in reporter.warnings)

    def test_a_real_typo_is_still_warned_about(self, catlist, config, reporter):
        config.blacklist_genres = ["Shootar"]
        catalog.categorize({}, catlist, config, reporter)
        assert any("Shootar" in text for text in reporter.warnings)


class TestOneGameOneRom:
    """Keeping a family's parent and setting its other versions aside.

    On the real 0.289 set this drops 7,538 alternate versions and 80 GB -- `dlair`
    alone ships four revisions at 11.5 GB each.
    """

    def family(self, **members):
        return {name: dict(description=name, cloneof=parent)
                for name, parent in members.items()}

    def kept(self, mame_list):
        return sorted(name for name, machine in mame_list.items()
                      if not machine.get("excluded"))

    def test_the_parent_is_kept_and_its_clones_are_not(self):
        result = catalog.collapse_clones(self.family(
            dlair=None, dlaire="dlair", dlairf="dlair"))
        assert self.kept(result) == ["dlair"]

    def test_a_dropped_version_says_why(self):
        result = catalog.collapse_clones(self.family(dlair=None, dlaire="dlair"))
        assert result["dlaire"]["clone_of_kept"] is True

    def test_a_family_whose_parent_did_not_survive_keeps_one_clone(self):
        # The parent may be a prototype, or have no ROMs of its own. Dropping the
        # clones too would lose the game entirely.
        result = catalog.collapse_clones(self.family(zclone="ghost", aclone="ghost"))
        assert self.kept(result) == ["aclone"]

    def test_which_clone_survives_is_predictable(self):
        one = catalog.collapse_clones(self.family(zz="ghost", mm="ghost", aa="ghost"))
        two = catalog.collapse_clones(self.family(aa="ghost", zz="ghost", mm="ghost"))
        assert self.kept(one) == self.kept(two) == ["aa"]

    def test_unrelated_machines_are_untouched(self):
        result = catalog.collapse_clones(self.family(galaga=None, pacman=None))
        assert self.kept(result) == ["galaga", "pacman"]

    def test_a_machine_already_excluded_stays_excluded(self):
        machines = self.family(dlair=None)
        machines["dlair"]["excluded"] = True
        result = catalog.collapse_clones(machines)
        assert result["dlair"]["excluded"] is True

    def test_it_says_how_many_it_set_aside(self, reporter):
        catalog.collapse_clones(self.family(dlair=None, dlaire="dlair"), reporter)
        assert any("1 alternate version" in text for text in reporter.messages)

    def test_nothing_to_collapse_says_nothing(self, reporter):
        catalog.collapse_clones(self.family(galaga=None), reporter)
        assert not any("alternate version" in text for text in reporter.messages)

    def test_an_empty_list_is_harmless(self):
        assert catalog.collapse_clones({}) == {}
