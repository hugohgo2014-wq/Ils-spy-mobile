namespace HelloWorld.Test;

/// <summary>
/// Small managed .NET assembly for testing ILSpy Mobile.
/// </summary>
public static class Greeter
{
    public static string SayHello(string name)
    {
        if (string.IsNullOrWhiteSpace(name))
            name = "ILSpy Mobile";

        return $"Hello, {name}! This code came from a managed .NET DLL.";
    }

    public static int Add(int left, int right) => left + right;

    public static int Factorial(int value)
    {
        if (value < 0)
            throw new ArgumentOutOfRangeException(nameof(value));

        var result = 1;
        for (var i = 2; i <= value; i++)
            result *= i;

        return result;
    }
}
