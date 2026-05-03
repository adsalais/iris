from iris import main


def test_main_runs_and_greets(capsys):
    main()
    assert capsys.readouterr().out == "Hello from iris!\n"
