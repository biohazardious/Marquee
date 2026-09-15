"""The genre/category tree's toggle rules, run as the browser would run them.

The rules are small but fiddly -- turning one category back on inside a genre that was
switched off wholesale has to leave its siblings off -- and getting them wrong makes the
page feel broken in exactly the way it did before.
"""
import os
import shutil
import subprocess
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "..", "marquee", "web", "static", "js", "selection.js")
START, END = "/* selection-logic:start", "/* selection-logic:end */"

node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def extract():
    with open(PAGE, encoding="utf-8") as handle:
        source = handle.read()
    body = source[source.index(START):source.index(END)]
    return body[body.index("\n") + 1:]


HARNESS = textwrap.dedent("""
    const S = {outGenres:new Set(), outCats:new Set(), outRoms:new Set(), romCat:{},
      cats:[{name:"Shooter / Gallery",genre:"Shooter",mature:false},
            {name:"Shooter / Flying",genre:"Shooter",mature:false},
            {name:"Maze / Misc",genre:"Maze",mature:false},
            {name:"Maze / Escape * Mature *",genre:"Maze",mature:true}],
      genres:[{name:"Shooter"},{name:"Maze"}],
      machines:{
        "Shooter / Gallery":[{name:"duckhunt",category:"Shooter / Gallery",genre:"Shooter"},
                             {name:"safari",  category:"Shooter / Gallery",genre:"Shooter"}],
        "Shooter / Flying":[{name:"1942",category:"Shooter / Flying",genre:"Shooter"}]},
      // What the rules are decided on: every name in a category, not the rows that
      // happen to have been fetched for display.
      members:{
        "Shooter / Gallery":["duckhunt","safari"],
        "Shooter / Flying":["1942"],
        "Maze / Misc":["pacman"],
        "Maze / Escape * Mature *":["naughty"]}};
    const changed = () => {};
    __LOGIC__
    const G = n => S.genres.find(g => g.name === n);
    const C = n => S.cats.find(c => c.name === n);
    // Every machine there is, independent of what happens to be drawn -- the point of
    // several of these tests is that the two are not the same thing.
    const ALL = {
      duckhunt:{name:"duckhunt",category:"Shooter / Gallery",genre:"Shooter"},
      safari:  {name:"safari",  category:"Shooter / Gallery",genre:"Shooter"},
      "1942":  {name:"1942",    category:"Shooter / Flying", genre:"Shooter"},
      pacman:  {name:"pacman",  category:"Maze / Misc",      genre:"Maze"},
      naughty: {name:"naughty", category:"Maze / Escape * Mature *", genre:"Maze"}};
    const M = n => ALL[n];
    const out = [];
    const g = (name, mature) => ({genre: name, mature: Boolean(mature)});
    const genreState = node => groupState(g(node.name));
    const toggleGenre = node => toggleGroup(g(node.name));
    const genreNeeds = node => groupNeeds(g(node.name));
    const step = label => out.push(label + "=" + genreState(G("Shooter")));
    const catStep = (label, c) => out.push(label + "=" + catState(C(c)));
    __STEPS__
    console.log(JSON.stringify({steps: out,
      outCats: [...S.outCats], outGenres: [...S.outGenres], outRoms: [...S.outRoms],
      maze: genreState(G("Maze"))}));
""")


def run(steps):
    script = HARNESS.replace("__LOGIC__", extract()).replace("__STEPS__", steps)
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                            timeout=30)
    assert result.returncode == 0, result.stderr
    import json
    return json.loads(result.stdout)


@node
class TestSelectionRules:
    def test_everything_starts_included(self):
        assert run('step("start")')["steps"] == ["start=in"]

    def test_a_genre_toggles_off_and_back_on(self):
        data = run('toggleGenre(G("Shooter")); step("off");'
                   'toggleGenre(G("Shooter")); step("on");')
        assert data["steps"] == ["off=out", "on=in"]

    def test_one_category_off_makes_the_genre_partial(self):
        data = run('toggleCat(C("Shooter / Gallery")); step("partial");')
        assert data["steps"] == ["partial=some"]
        assert data["outCats"] == ["Shooter / Gallery"]

    def test_a_category_goes_back_on(self):
        data = run('toggleCat(C("Shooter / Gallery"));'
                   'toggleCat(C("Shooter / Gallery")); step("back");')
        assert data["steps"] == ["back=in"]
        assert data["outCats"] == []

    def test_reopening_one_category_of_a_switched_off_genre(self):
        """The other categories must stay out, and the genre-wide flag must clear."""
        data = run('toggleGenre(G("Shooter"));'
                   'toggleCat(C("Shooter / Flying")); step("partial");')
        assert data["steps"] == ["partial=some"]
        assert data["outCats"] == ["Shooter / Gallery"]
        assert data["outGenres"] == []

    def test_that_state_is_fully_reversible(self):
        data = run('toggleGenre(G("Shooter"));'
                   'toggleCat(C("Shooter / Flying"));'
                   'toggleCat(C("Shooter / Gallery")); step("all back");')
        assert data["steps"] == ["all back=in"]
        assert data["outCats"] == [] and data["outGenres"] == []

    def test_other_genres_are_untouched(self):
        data = run('toggleGenre(G("Shooter"));'
                   'toggleCat(C("Shooter / Flying")); step("x");')
        assert data["maze"] == "in"


@node
class TestGameRules:
    """Unticking one game, and the reverse."""

    def test_one_game_off_makes_its_category_partial(self):
        data = run('toggleMachine(M("duckhunt"));'
                   'catStep("cat", "Shooter / Gallery"); step("genre");')
        assert data["steps"] == ["cat=some", "genre=some"]
        assert data["outRoms"] == ["duckhunt"]

    def test_a_game_goes_back_on(self):
        data = run('toggleMachine(M("duckhunt")); toggleMachine(M("duckhunt"));'
                   'catStep("cat", "Shooter / Gallery");')
        assert data["steps"] == ["cat=in"]
        assert data["outRoms"] == []

    def test_unticking_a_category_covers_its_games(self):
        data = run('toggleCat(C("Shooter / Gallery"));'
                   'out.push("game=" + machineOut(M("duckhunt")));')
        assert data["steps"] == ["game=true"]

    def test_ticking_one_game_of_an_excluded_category_leaves_its_siblings_out(self):
        data = run('toggleCat(C("Shooter / Gallery"));'
                   'toggleMachine(M("duckhunt"));'
                   'catStep("cat", "Shooter / Gallery");')
        assert data["steps"] == ["cat=some"]
        assert data["outRoms"] == ["safari"]
        assert data["outCats"] == []

    def test_ticking_one_game_of_an_excluded_genre_leaves_everything_else_out(self):
        data = run('toggleGenre(G("Shooter"));'
                   'toggleMachine(M("duckhunt")); step("genre");')
        assert data["steps"] == ["genre=some"]
        assert data["outGenres"] == []
        assert data["outCats"] == ["Shooter / Flying"]
        assert data["outRoms"] == ["safari"]

    def test_ticking_a_partly_full_category_fills_it(self):
        """A half-ticked box fills on click, as a tri-state box anywhere else does."""
        data = run('toggleMachine(M("duckhunt"));'
                   'catStep("before", "Shooter / Gallery");'
                   'toggleCat(C("Shooter / Gallery"));'
                   'catStep("after", "Shooter / Gallery");')
        assert data["steps"] == ["before=some", "after=in"]
        assert data["outRoms"] == []

    def test_a_category_emptied_one_game_at_a_time_reads_as_empty(self):
        """Not "some". It is the same category as one switched off wholesale, and a
        box that still looks half-full puts everything back when clicked."""
        data = run('toggleMachine(M("duckhunt")); toggleMachine(M("safari"));'
                   'catStep("before", "Shooter / Gallery");'
                   'toggleCat(C("Shooter / Gallery"));'
                   'catStep("after", "Shooter / Gallery");')
        assert data["steps"] == ["before=out", "after=in"]
        assert data["outRoms"] == []

    def test_and_clicking_a_full_category_empties_it(self):
        data = run('toggleCat(C("Shooter / Gallery"));'
                   'catStep("after", "Shooter / Gallery");')
        assert data["steps"] == ["after=out"]

    def test_ticking_a_partly_full_genre_clears_everything_under_it(self):
        data = run('toggleMachine(M("duckhunt"));'
                   'toggleCat(C("Shooter / Flying"));'
                   'step("mixed");'
                   'toggleGenre(G("Shooter")); step("back");')
        assert data["steps"] == ["mixed=some", "back=in"]
        assert data["outRoms"] == [] and data["outCats"] == []


@node
class TestBulk:
    """Acting on a whole filtered list, which is how a download list is managed.

    It goes through the same per-game rule rather than a second set of its own: a
    shortcut here would be a second place for "turning one game back on inside a genre
    that is off must not drag its siblings back" to be got wrong.
    """

    ALL = 'const L = Object.values(S.machines).flat();'

    def test_leaving_every_match_out(self):
        data = run(self.ALL + 'setAll(L, false); step("after");')
        assert sorted(data["outRoms"]) == ["1942", "duckhunt", "safari"]
        # Every game in the genre gone, one at a time, is a genre that is gone.
        assert data["steps"] == ["after=out"]

    def test_putting_every_match_back(self):
        data = run(self.ALL + 'setAll(L, false); setAll(L, true); step("after");')
        assert data["outRoms"] == []
        assert data["steps"] == ["after=in"]

    def test_it_does_not_disturb_what_already_agrees(self):
        data = run(self.ALL + 'setAll(L, true); step("after");')
        assert data["outRoms"] == []
        assert data["outGenres"] == []
        assert data["steps"] == ["after=in"]

    def test_putting_some_back_inside_a_genre_that_is_off_leaves_the_rest_off(self):
        """The rule the whole tree exists for, applied in bulk."""
        data = run('toggleGenre(G("Shooter"));'
                   'setAll(S.machines["Shooter / Gallery"], true);'
                   'step("after"); catStep("gallery", "Shooter / Gallery");'
                   'catStep("flying", "Shooter / Flying");')
        assert data["steps"] == ["after=some", "gallery=in", "flying=out"]
        assert "Shooter" not in data["outGenres"]

    def test_an_empty_list_changes_nothing(self):
        data = run('toggleCat(C("Shooter / Gallery")); setAll([], false);'
                   'catStep("still", "Shooter / Gallery");')
        assert data["steps"] == ["still=out"]


@node
class TestMembershipIsComplete:
    """The rules are decided on a category's whole membership, never on the rows that
    happen to have been fetched.

    Only the largest 500 of a category are ever drawn, and Slot Machine / Video Slot
    has 965. Judging the tick box on the drawn ones reported the wrong state, and
    pushing an exclusion down onto "the siblings" quietly lost the other 465.
    """

    def test_a_category_is_judged_on_all_of_it_not_the_drawn_rows(self):
        """Half the category is drawn; unticking every drawn game is not the category."""
        data = run('S.machines["Shooter / Gallery"] = [M("duckhunt")];'
                   'toggleMachine(M("duckhunt"));'
                   'catStep("half", "Shooter / Gallery");')
        assert data["steps"] == ["half=some"]

    def test_putting_one_back_keeps_every_sibling_out_not_just_the_drawn_ones(self):
        data = run('S.machines["Shooter / Gallery"] = [];'   # nothing drawn at all
                   'toggleCat(C("Shooter / Gallery"));'
                   'toggleMachine(M("duckhunt"));'
                   'catStep("after", "Shooter / Gallery");')
        assert data["outRoms"] == ["safari"], "the undrawn sibling came back"
        assert data["steps"] == ["after=some"]

    def test_switching_a_category_back_on_clears_all_of_it(self):
        data = run('setAll(S.machines["Shooter / Gallery"], false);'
                   'toggleCat(C("Shooter / Gallery"));'
                   'catStep("after", "Shooter / Gallery");')
        assert data["outRoms"] == []
        assert data["steps"] == ["after=in"]

    def test_a_category_nothing_is_known_about_reads_as_in(self):
        data = run('delete S.members["Maze / Misc"]; catStep("unknown", "Maze / Misc");')
        assert data["steps"] == ["unknown=in"]


@node
class TestWhatEachToggleNeedsFirst:
    """Which memberships have to be in hand before a rule may run.

    Unticking never needs any -- it adds one name to a list. Ticking does, because it
    has to push the exclusion it clears down onto siblings, and it cannot push onto
    what it has not got.
    """

    def test_unticking_needs_nothing(self):
        data = run('out.push("genre=" + JSON.stringify(genreNeeds(G("Shooter"))));'
                   'out.push("cat=" + JSON.stringify(catNeeds(C("Shooter / Flying"))));'
                   'out.push("game=" + JSON.stringify(needsMembers(M("1942"))));')
        assert data["steps"] == ["genre=[]", "cat=[]", "game=[]"]

    def test_ticking_a_genre_back_needs_the_whole_genre(self):
        data = run('toggleGenre(G("Shooter"));'
                   'out.push(JSON.stringify(genreNeeds(G("Shooter"))));')
        assert data["steps"] == ['[{"genre":"Shooter"}]']

    def test_ticking_a_category_back_needs_that_category(self):
        data = run('toggleCat(C("Shooter / Flying"));'
                   'out.push(JSON.stringify(catNeeds(C("Shooter / Flying"))));')
        assert data["steps"] == ['[{"category":"Shooter / Flying"}]']

    def test_a_game_asks_for_whichever_level_is_hiding_it(self):
        data = run('toggleGenre(G("Shooter"));'
                   'out.push("bygenre=" + JSON.stringify(needsMembers(M("1942"))));'
                   'toggleMachine(M("1942"));'
                   'toggleMachine(M("1942"));'
                   'out.push("byself=" + JSON.stringify(needsMembers(M("1942"))));')
        assert data["steps"] == ['bygenre=[{"genre":"Shooter"}]', 'byself=[]']


@node
class TestTheAdultBranch:
    """catlist marks adult categories in the category name itself, so "Maze / Escape
    * Mature *" is already a different category from the ordinary Maze ones and is
    filed under its own folder. The tree shows that split; these are the rules that
    keep the two halves from dragging each other about.
    """

    ADULT = 'const A = {genre:"Maze", mature:true};'
    PLAIN = 'const P = {genre:"Maze", mature:false};'

    def test_the_two_halves_of_a_genre_are_separate(self):
        data = run(self.ADULT + self.PLAIN
                   + 'toggleGroup(A);'
                   + 'out.push("adult=" + groupState(A));'
                   + 'out.push("plain=" + groupState(P));')
        assert data["steps"] == ["adult=out", "plain=in"]
        assert data["outCats"] == ["Maze / Escape * Mature *"]

    def test_switching_the_plain_half_off_leaves_the_adult_one_alone(self):
        data = run(self.ADULT + self.PLAIN
                   + 'toggleGroup(P);'
                   + 'out.push("plain=" + groupState(P));'
                   + 'out.push("adult=" + groupState(A));')
        assert data["steps"] == ["plain=out", "adult=in"]
        # Not recorded as a whole genre: that would take the adult half with it.
        assert data["outGenres"] == []
        assert data["outCats"] == ["Maze / Misc"]

    def test_a_genre_with_only_one_half_is_still_recorded_as_a_genre(self):
        """Shooter has no adult categories, so the short form is safe and is used."""
        data = run('toggleGroup({genre:"Shooter", mature:false});'
                   'out.push("shooter=" + groupState({genre:"Shooter", mature:false}));')
        assert data["outGenres"] == ["Shooter"]
        assert data["outCats"] == []

    def test_a_whole_genre_exclusion_is_pushed_down_when_one_half_comes_back(self):
        """The saved settings hold whole genres; turning the adult half back on must
        not bring the ordinary half with it."""
        data = run(self.ADULT + self.PLAIN
                   + 'S.outGenres.add("Maze");'
                   + 'toggleGroup(A);'
                   + 'out.push("adult=" + groupState(A));'
                   + 'out.push("plain=" + groupState(P));')
        assert data["steps"] == ["adult=in", "plain=out"]
        assert data["outGenres"] == []
        assert data["outCats"] == ["Maze / Misc"]

    def test_the_adult_branch_switches_off_as_one(self):
        data = run('toggleSide(true);'
                   'out.push("adult=" + sideState(true));'
                   'out.push("plain=" + sideState(false));')
        assert data["steps"] == ["adult=out", "plain=in"]

    def test_and_back_on_again(self):
        data = run('toggleSide(true); toggleSide(true);'
                   'out.push("adult=" + sideState(true));')
        assert data["steps"] == ["adult=in"]
        assert data["outCats"] == []

    def test_one_adult_game_still_belongs_to_its_own_category(self):
        data = run(self.ADULT
                   + 'toggleGroup(A); toggleMachine(M("naughty"));'
                   + 'out.push("adult=" + groupState(A));'
                   + 'out.push("plain=" + groupState({genre:"Maze", mature:false}));')
        assert data["steps"] == ["adult=in", "plain=in"]
        assert data["outRoms"] == []
