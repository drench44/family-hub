#!/usr/bin/env python3
"""Real-wall smoke test for the on-screen keyboard.

The wall is a touchscreen running Firefox under Wayland (see
docs/on-screen-keyboard.md). CI can't reach it, and the fake-DOM/static guards
can't see real Firefox behaviour - so a batch of real bugs (scattered text,
keyboard not re-summoning after Cancel) only showed up on the actual device.
This drives the REAL Firefox against a running hub with Marionette (Firefox's
official automation) and asserts the behaviour that only shows up there.

It hard-codes no addresses: pass the hub URL. Run it wherever a Firefox that can
reach the hub lives (typically on the wall itself, over SSH):

    python3 wall-smoke-test.py --hub-url http://<your-hub>:8138 \
        --firefox /usr/lib/firefox-esr/firefox-esr

Needs `marionette_driver` (pip install marionette_driver), ideally in a venv.
Exits non-zero if any check fails, so it can gate a UI deploy.
"""
import argparse
import base64
import os
import sys
import time

DOG = "\U0001F436"  # a color-verified emoji from the picker


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hub-url", default=os.environ.get("HUB_URL"),
                    help="Base URL of the running hub, e.g. http://host:8138")
    ap.add_argument("--firefox", default=os.environ.get("FIREFOX_BIN",
                    "/usr/lib/firefox-esr/firefox-esr"),
                    help="Path to the Firefox binary to drive")
    ap.add_argument("--shots-dir", default=os.environ.get("SHOTS_DIR", "/tmp"),
                    help="Where to save screenshots")
    args = ap.parse_args()
    if not args.hub_url:
        ap.error("--hub-url (or HUB_URL) is required")
    hub = args.hub_url.rstrip("/")

    from marionette_driver.marionette import Marionette
    c = Marionette(bin=args.firefox, headless=True)
    c.start_session()
    c.set_window_rect(x=0, y=0, width=1920, height=1080)

    results = []

    def check(name, ok):
        results.append(bool(ok))
        print(("PASS " if ok else "FAIL ") + name)
        sys.stdout.flush()

    def js(s):
        return c.execute_script(s)

    def shot(name):
        with open(os.path.join(args.shots_dir, name), "wb") as f:
            f.write(base64.b64decode(c.screenshot()))

    def wait(expr, t=12):
        end = time.time() + t
        while time.time() < end:
            try:
                if c.execute_script("return (" + expr + ")"):
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    OSK_VIS = ("(function(){var o=document.querySelector('.osk');"
               "return !!o && !o.classList.contains('hidden');})()")
    MARK = "wall_smoke_delete_me"   # marker to-do; deleted in finally, always

    def cleanup_marker():
        # Delete the marker to-do if it was created - runs in finally so a
        # mid-run failure never leaves a stray item on the family's real list.
        try:
            body = js("var x=new XMLHttpRequest();x.open('GET','/api/todos',false);x.send();return x.responseText;")
            if MARK in (body or ""):
                js("var x=new XMLHttpRequest();x.open('GET','/api/todos',false);x.send();var d=JSON.parse(x.responseText);"
                   "var id=null;(function w(o){if(Array.isArray(o))o.forEach(w);else if(o&&typeof o=='object'){"
                   "if(o.title===%r&&o.id)id=o.id;Object.values(o).forEach(w);}})(d);"
                   "if(id){var y=new XMLHttpRequest();y.open('DELETE','/api/todos/'+id,false);y.send();}return id;" % MARK)
                print("cleanup: removed the marker to-do")
        except Exception:
            pass

    def tapkey(k):
        return js("var e=document.querySelectorAll('.osk-keys .osk-key');"
                  "for(var i=0;i<e.length;i++){if(e[i].dataset.key===%r){e[i].click();return true;}}"
                  "return false;" % k)

    def tapsel(s):
        return js("var e=document.querySelector(%r); if(e){e.click();return true;} return false;" % s)

    def val():
        return js("return document.getElementById('todo-add-input').value")

    def todo_count():
        return js("var x=new XMLHttpRequest();x.open('GET','/api/todos',false);x.send();"
                  "try{var d=JSON.parse(x.responseText);var n=0;(function w(o){if(Array.isArray(o))o.forEach(w);"
                  "else if(o&&typeof o=='object'){if(typeof o.title=='string')n++;Object.values(o).forEach(w);}})(d);"
                  "return n;}catch(e){return -1;}")

    try:
        # A. main page renders un-scattered (a bad emoji fontconfig once blew the
        #    clock digits into full-width emoji cells).
        c.navigate(hub + "/")
        wait("!!document.getElementById('clock-time') && document.getElementById('clock-time').textContent.length>0")
        time.sleep(1.5)
        shot("wall-main.png")
        w = js("var e=document.getElementById('clock-time'); return e? e.getBoundingClientRect().width : -1")
        txt = js("var e=document.getElementById('clock-time'); return e? e.textContent : ''")
        check("main page text renders compact (not scattered): '%s' width=%s" % (txt, round(w)), 0 < w < 320)

        # B. keyboard flow with ?kiosk=1
        c.navigate(hub + "/?kiosk=1")
        check("keyboard builds in kiosk mode", wait("!!document.querySelector('.osk')"))
        js("var b=document.querySelector('[data-overlay=\"todos\"]'); if(b) b.click();")
        check("todo add-input appears", wait("!!document.getElementById('todo-add-input')"))
        time.sleep(1.2)
        js("document.getElementById('todo-add-input').focus();")
        check("keyboard shows on focus", wait(OSK_VIS))
        tapkey('h'); tapkey('i')
        check("typing works: '%s'" % val(), val() == 'hi')
        tapsel('.osk-key[data-key="Layer:emoji"]')
        check("emoji picker has category tabs", wait("document.querySelectorAll('.osk-emoji-cat').length>=10"))
        tapsel('.osk-key[data-key="EmojiCat:animals"]')
        wait("!!document.querySelector('.osk-emoji[data-key=\"%s\"]')" % DOG)
        shot("wall-emoji.png")
        tapsel('.osk-emoji[data-key="%s"]' % DOG)
        check("emoji inserts whole: '%s'" % val(), val() == 'hi' + DOG)
        tapkey('Backspace')
        check("backspace deletes whole emoji: '%s'" % val(), val() == 'hi')
        tapsel('.osk-key[data-key="Layer:letters"]')

        # C. Cancel discards + hides, then the SAME field re-summons the keyboard,
        #    then Done submits. (The re-summon path is the real-wall regression.)
        tapkey('Cancel')
        check("Cancel clears + hides", val() == '' and not js("return " + OSK_VIS))
        js("var i=document.getElementById('todo-add-input'); i.blur(); i.focus();")  # a fresh tap
        check("keyboard re-summons on re-tap after Cancel", wait(OSK_VIS))

        before = todo_count()
        js("var i=document.getElementById('todo-add-input'); i.value=%r;"
           "i.dispatchEvent(new Event('input',{bubbles:true}));" % MARK)
        tapkey('Done')
        time.sleep(2.0)
        after = todo_count()
        check("Done adds a to-do (%s->%s)" % (before, after), after == before + 1)

        passed = sum(results)
        print("\nSUMMARY: %d/%d passed" % (passed, len(results)))
        return 0 if passed == len(results) else 1
    finally:
        cleanup_marker()
        try:
            c.delete_session()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
