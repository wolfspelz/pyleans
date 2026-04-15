using My.Interfaces;

namespace My.Logic;

[GenerateSerializer]
public class FieldState
{
    [Id(0)] public bool Selected { get; set; } = false;
}

public class Field : Grain, IField
{
    private readonly IPersistentState<FieldState> _state;
    private readonly string _color;
    private readonly DateTime _createdAt;

    public Field(
        [PersistentState("field", "JsonFileStorage")] IPersistentState<FieldState> state
    )
    {
        _state = state;
        _createdAt = DateTime.UtcNow;

        _color = "coral";
        // _color = "cornflowerblue"; 
    }

    public async Task<FieldProperties> Get()
    {
        return await Task.FromResult(new FieldProperties
        {
            Color = _color,
            Count = (int)(DateTime.UtcNow - _createdAt).TotalSeconds,
            Selected = _state.State.Selected,
        });
    }

    public async Task Select(bool value)
    {
        _state.State.Selected = value;
        await _state.WriteStateAsync();
    }
}
