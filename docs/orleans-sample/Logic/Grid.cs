using My.Interfaces;

namespace My.Logic;

public class Grid : Grain, IGrid
{
    public Grid(
    )
    {
    }

    public async Task<FieldProperties[][]> Get(int nx, int ny) {
        var id = this.GetPrimaryKeyString();

        FieldProperties[][] result = new FieldProperties[ny][];
        for (int iy = 0; iy < ny; iy++) {
            result[iy] = new FieldProperties[nx];
            for (int ix = 0; ix < nx; ix++) {
                var grain = GrainFactory.GetGrain<IField>($"{id}-{ix}-{iy}");
                result[iy][ix] = await grain.Get();
            }
        }
        return result;
    }
}
